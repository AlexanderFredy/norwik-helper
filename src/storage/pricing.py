"""SQLite: состояние диалога и подготовленные к записи предложения по ценам.

`pending_proposal` — ключевой элемент безопасности (§10 спеки): агент формирует payload
`set-prices`, код его сохраняет, а отправляет в 1С **только** обработчик кнопки
подтверждения. Модель не может ни записать цены, ни изменить payload после подтверждения:
на кнопку уходит ровно то, что админ видел в предложении.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# Ключ — (сигнатура, ЛИСТ). У мультилистового прайса («Плинтус», «LA», «SPC LVT») колонки
# на каждом листе свои, и одна строка на файл означала бы, что запоминается только
# последний разобранный лист, а остальные молча выпадают из обработки.
_MAPPINGS_TABLE = """
CREATE TABLE IF NOT EXISTS price_mappings (
    signature  TEXT NOT NULL,           -- сигнатура структуры прайса (§6.5.2)
    sheet      TEXT NOT NULL DEFAULT '',-- имя листа; '' — прайс из одного листа
    supplier   TEXT,                    -- как назывался поставщик — для показа админу
    mapping    TEXT NOT NULL,           -- JSON: колонки цен, база
    updated_at TEXT NOT NULL,
    uses       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (signature, sheet)
);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dialog_state (
    user_id    INTEGER PRIMARY KEY,
    messages   TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    turns      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending_proposal (
    proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    payload     TEXT NOT NULL,          -- items для set-prices
    summary     TEXT NOT NULL,          -- что показали админу
    item_count  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'   -- pending | applied | rejected
);
CREATE INDEX IF NOT EXISTS ix_pending_user ON pending_proposal (user_id, status);
{mappings}
-- Прогон прайса по маркам (§9.6): что запланировали и что уже записали. Живёт между
-- ходами диалога И нажатиями кнопки, поэтому в БД, а не в памяти процесса.
CREATE TABLE IF NOT EXISTS price_run (
    user_id    INTEGER PRIMARY KEY,
    supplier   TEXT,
    price_doc  TEXT,
    planned    TEXT NOT NULL,       -- JSON: [{code, name}] в порядке обработки
    done       TEXT NOT NULL,       -- JSON: [code] уже обработанных
    started_at TEXT NOT NULL
);
-- Категории товаров, которые вообще анализируем (§6.8). Пусто = ограничений нет.
-- Задаётся один раз на все прайсы, а не на каждый файл.
CREATE TABLE IF NOT EXISTS product_scope (
    category_norm TEXT PRIMARY KEY,     -- нормализованное имя — по нему сравниваем
    category      TEXT NOT NULL,        -- как показывать админу
    added_at      TEXT NOT NULL
);
-- Журнал наших записей цен: 1С хранит дату изменения, но не источник. Только отсюда
-- известно, из какого прайса цена взялась (dev_tasks п.6).
CREATE TABLE IF NOT EXISTS price_writes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    written_on     TEXT NOT NULL,       -- день записи = период регистра цен 1С
    tm_code        TEXT,
    tm_name        TEXT,
    collection_ref TEXT,
    collection     TEXT,
    item_ref       TEXT,
    item_name      TEXT,
    price_type     TEXT NOT NULL,
    old_value      REAL,
    new_value      REAL,
    supplier       TEXT,
    price_doc      TEXT,                -- имя файла прайса
    price_date     TEXT                 -- дата самого прайса, не записи
);
CREATE INDEX IF NOT EXISTS ix_price_writes_item ON price_writes (item_ref, written_on);
CREATE INDEX IF NOT EXISTS ix_price_writes_day ON price_writes (written_on);
-- Заявки поставщиков об эксклюзиве (§9.5). Пишутся при КАЖДОМ разборе прайса, в т.ч.
-- отклонённого: «эксклюзив» — факт из прайса, а не следствие решения о записи цен.
-- Никогда не перезаписываются: действующая пометка выводится из них (price_tool/exclusive).
CREATE TABLE IF NOT EXISTS exclusive_claims (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier       TEXT NOT NULL,       -- пока текстом, как в price_writes; supplier_id — с §4.5
    tm_code        TEXT NOT NULL,
    tm_name        TEXT NOT NULL DEFAULT '',
    collection_ref TEXT NOT NULL DEFAULT '',   -- '' = заявка на всю ТМ
    collection     TEXT NOT NULL DEFAULT '',
    item_ref       TEXT NOT NULL DEFAULT '',   -- '' = заявка на коллекцию целиком
    item_name      TEXT NOT NULL DEFAULT '',
    phrase         TEXT NOT NULL DEFAULT '',   -- дословно из прайса — по нему админ судит спор
    where_found    TEXT NOT NULL DEFAULT '',   -- column | header | sheet | filename | admin
    price_date     TEXT NOT NULL,              -- дата прайса: по ней считается окно спора
    price_doc      TEXT,
    recorded_at    TEXT NOT NULL,
    UNIQUE (supplier, tm_code, collection_ref, item_ref, price_date)
);
CREATE INDEX IF NOT EXISTS ix_exclusive_claims_tm ON exclusive_claims (tm_code, price_date);
-- Решение админа. supplier IS NULL = «эксклюзива нет», пометка снимается насовсем.
CREATE TABLE IF NOT EXISTS exclusive_decisions (
    tm_code        TEXT NOT NULL,
    collection_ref TEXT NOT NULL DEFAULT '',
    item_ref       TEXT NOT NULL DEFAULT '',
    supplier       TEXT,
    decided_at     TEXT NOT NULL,
    note           TEXT,
    PRIMARY KEY (tm_code, collection_ref, item_ref)
);
""".replace("{mappings}", _MAPPINGS_TABLE)

DIALOG_TTL_MINUTES = 60


@dataclass(frozen=True)
class Proposal:
    proposal_id: int
    user_id: int
    payload: list[dict]
    summary: str
    item_count: int
    digest: dict | None = None     # снимок «было → стало» для рассылки и журнала (п.6)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PricingStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            # база уже могла быть создана до появления дайджеста — дописываем колонку
            cur = await db.execute("PRAGMA table_info(pending_proposal)")
            columns = {row[1] for row in await cur.fetchall()}
            if "digest" not in columns:
                await db.execute("ALTER TABLE pending_proposal ADD COLUMN digest TEXT")
            await self._migrate_mappings(db)
            await db.commit()

    @staticmethod
    async def _migrate_mappings(db) -> None:
        """Ключ маппинга: сигнатура → (сигнатура, лист).

        В старой схеме на файл приходилась одна строка, а имя листа лежало внутри JSON.
        Переносим его в колонку: мультилистовой прайс должен помнить каждый лист отдельно.
        """
        cur = await db.execute("PRAGMA table_info(price_mappings)")
        columns = {row[1] for row in await cur.fetchall()}
        if not columns or "sheet" in columns:
            return
        cur = await db.execute(
            "SELECT signature, supplier, mapping, updated_at, uses FROM price_mappings")
        rows = await cur.fetchall()
        await db.execute("DROP TABLE price_mappings")
        await db.executescript(_MAPPINGS_TABLE)
        for signature, supplier, mapping, updated_at, uses in rows:
            try:
                sheet = (json.loads(mapping) or {}).get("sheet") or ""
            except (ValueError, AttributeError):
                sheet = ""
            await db.execute(
                "INSERT OR IGNORE INTO price_mappings (signature, sheet, supplier, mapping, "
                "updated_at, uses) VALUES (?, ?, ?, ?, ?, ?)",
                (signature, sheet, supplier, mapping, updated_at, uses))
        logger.info("Маппинги колонок переведены на ключ (сигнатура, лист): %d", len(rows))

    # ------------------------------------------------------------------ диалог

    async def load_messages(self, user_id: int) -> list[dict]:
        """Сообщения диалога; при простое дольше TTL история считается устаревшей."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT messages, updated_at FROM dialog_state WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
        if not row:
            return []
        try:
            updated = datetime.fromisoformat(row[1])
        except ValueError:
            return []
        age_min = (datetime.now(timezone.utc) - updated).total_seconds() / 60
        if age_min > DIALOG_TTL_MINUTES:
            return []
        return json.loads(row[0])

    async def save_messages(self, user_id: int, messages: list[dict]) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO dialog_state (user_id, messages, updated_at, turns) "
                "VALUES (?, ?, ?, 1) ON CONFLICT(user_id) DO UPDATE SET "
                "messages = excluded.messages, updated_at = excluded.updated_at, "
                "turns = dialog_state.turns + 1",
                (user_id, json.dumps(messages, ensure_ascii=False), _now()))
            await db.commit()

    async def reset(self, user_id: int) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM dialog_state WHERE user_id = ?", (user_id,))
            await db.execute("UPDATE pending_proposal SET status = 'rejected' "
                             "WHERE user_id = ? AND status = 'pending'", (user_id,))
            await db.commit()

    # ------------------------------------------------------- предложения к записи

    async def save_proposal(self, user_id: int, payload: list[dict], summary: str,
                            digest: dict | None = None) -> int:
        """Новое предложение заменяет предыдущее неподтверждённое."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("UPDATE pending_proposal SET status = 'rejected' "
                             "WHERE user_id = ? AND status = 'pending'", (user_id,))
            cur = await db.execute(
                "INSERT INTO pending_proposal (user_id, payload, summary, item_count, "
                "created_at, digest) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, json.dumps(payload, ensure_ascii=False), summary, len(payload),
                 _now(), json.dumps(digest, ensure_ascii=False) if digest else None))
            await db.commit()
            return cur.lastrowid

    async def get_pending(self, user_id: int) -> Proposal | None:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT proposal_id, payload, summary, item_count, digest FROM pending_proposal "
                "WHERE user_id = ? AND status = 'pending' ORDER BY proposal_id DESC LIMIT 1",
                (user_id,))
            row = await cur.fetchone()
        if not row:
            return None
        return Proposal(proposal_id=row[0], user_id=user_id, payload=json.loads(row[1]),
                        summary=row[2], item_count=row[3],
                        digest=json.loads(row[4]) if row[4] else None)

    async def take_pending(self, user_id: int, proposal_id: int) -> Proposal | None:
        """Атомарно берёт предложение в работу: повторное нажатие кнопки не сработает.

        Статус промежуточный (`applying`) — запись в 1С ещё не выполнена. По её итогу
        вызывается `mark_applied` или `release` (1С периодически рвёт соединение, и терять
        из-за этого разобранный прайс нельзя).
        """
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE pending_proposal SET status = 'applying' "
                "WHERE proposal_id = ? AND user_id = ? AND status = 'pending' "
                "RETURNING payload, summary, item_count, digest",
                (proposal_id, user_id))
            row = await cur.fetchone()
            await db.commit()
        if not row:
            return None
        return Proposal(proposal_id=proposal_id, user_id=user_id, payload=json.loads(row[0]),
                        summary=row[1], item_count=row[2],
                        digest=json.loads(row[3]) if row[3] else None)

    async def mark_applied(self, proposal_id: int) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("UPDATE pending_proposal SET status = 'applied' "
                             "WHERE proposal_id = ?", (proposal_id,))
            await db.commit()

    async def release(self, proposal_id: int) -> None:
        """Вернуть предложение в очередь — запись не состоялась, кнопку можно нажать снова."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("UPDATE pending_proposal SET status = 'pending' "
                             "WHERE proposal_id = ? AND status = 'applying'", (proposal_id,))
            await db.commit()

    # -------------------------------------------------------- журнал записей (п.6)

    async def record_writes(self, rows: list[dict]) -> int:
        """Запомнить, что именно и из какого прайса записано. Пишется после ответа 1С."""
        if not rows:
            return 0
        fields = ("written_on", "tm_code", "tm_name", "collection_ref", "collection",
                  "item_ref", "item_name", "price_type", "old_value", "new_value",
                  "supplier", "price_doc", "price_date")
        async with aiosqlite.connect(self._db_path) as db:
            await db.executemany(
                f"INSERT INTO price_writes ({', '.join(fields)}) "
                f"VALUES ({', '.join('?' * len(fields))})",
                [tuple(r.get(f) for f in fields) for r in rows])
            await db.commit()
        return len(rows)

    async def price_sources(self, item_refs: list[str], dates: list[str]) -> dict:
        """{(item_ref, дата): {supplier, price_doc, price_date}} — откуда взялась цена.

        Ключ включает дату: цена, записанная нами 20.07, объясняется прайсом только если
        1С показывает изменение именно за 20.07. Совпадения нет — значит правили мимо нас.
        """
        refs = [r for r in dict.fromkeys(item_refs) if r]
        days = [d for d in dict.fromkeys(dates) if d]
        if not refs or not days:
            return {}
        out: dict = {}
        async with aiosqlite.connect(self._db_path) as db:
            for i in range(0, len(refs), 400):      # предел числа параметров SQLite
                chunk = refs[i:i + 400]
                cur = await db.execute(
                    "SELECT item_ref, written_on, supplier, price_doc, price_date "
                    "FROM price_writes WHERE item_ref IN "
                    f"({', '.join('?' * len(chunk))}) AND written_on IN "
                    f"({', '.join('?' * len(days))}) ORDER BY id",
                    (*chunk, *days))
                for ref, day, supplier, doc, price_date in await cur.fetchall():
                    out[(ref, day)] = {"supplier": supplier, "price_doc": doc,
                                       "price_date": price_date}
        return out

    # ------------------------------------------------------------ эксклюзивы (§9.5)

    async def record_exclusive_claims(self, claims: list[dict]) -> int:
        """Заявки об эксклюзиве из разобранного прайса.

        Повторный разбор того же прайса ничего не добавляет: ключ (поставщик, объект,
        дата прайса) уникален, конфликт игнорируется.
        """
        if not claims:
            return 0
        # price_doc допускает NULL, остальные объявлены NOT NULL — пустая строка вместо None
        fields = ("supplier", "tm_code", "tm_name", "collection_ref", "collection",
                  "item_ref", "item_name", "phrase", "where_found", "price_date")
        now = _now()
        rows = [tuple(c.get(f) or "" for f in fields) + (c.get("price_doc"), now)
                for c in claims]
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.executemany(
                f"INSERT OR IGNORE INTO exclusive_claims ({', '.join(fields)}, "
                f"price_doc, recorded_at) VALUES ({', '.join('?' * (len(fields) + 2))})",
                rows)
            await db.commit()
            return cur.rowcount

    async def load_exclusives(self, ttl_days: int = 365) -> tuple[list, list]:
        """(заявки не старше ttl, решения админа) — вход для `exclusive.resolve`."""
        from src.price_tool.exclusive import Claim, Decision

        horizon = (datetime.now(timezone.utc).date() - timedelta(days=ttl_days)).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT supplier, tm_code, tm_name, collection_ref, collection, item_ref, "
                "item_name, phrase, where_found, price_date, price_doc FROM exclusive_claims "
                "WHERE price_date >= ? ORDER BY price_date", (horizon,))
            claims = [Claim(*row) for row in await cur.fetchall()]
            cur = await db.execute(
                "SELECT tm_code, collection_ref, item_ref, supplier, decided_at, note "
                "FROM exclusive_decisions")
            decisions = [Decision(tm_code=r[0], collection_ref=r[1], item_ref=r[2],
                                  supplier=r[3], decided_at=r[4], note=r[5] or "")
                         for r in await cur.fetchall()]
        return claims, decisions

    async def set_exclusive_decision(self, tm_code: str, collection_ref: str = "",
                                     item_ref: str = "", supplier: str | None = None,
                                     note: str | None = None) -> None:
        """Ответ админа. `supplier=None` снимает пометку и глушит будущие заявки."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO exclusive_decisions (tm_code, collection_ref, item_ref, "
                "supplier, decided_at, note) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tm_code, collection_ref, item_ref) DO UPDATE SET "
                "supplier = excluded.supplier, decided_at = excluded.decided_at, "
                "note = excluded.note",
                (tm_code, collection_ref or "", item_ref or "", supplier or None,
                 _now()[:10], note))
            await db.commit()

    async def clear_exclusive_decision(self, tm_code: str, collection_ref: str = "",
                                       item_ref: str = "") -> bool:
        """Убрать решение — объект снова определяется по заявкам поставщиков."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM exclusive_decisions WHERE tm_code = ? AND collection_ref = ? "
                "AND item_ref = ?", (tm_code, collection_ref or "", item_ref or ""))
            await db.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------- маппинг колонок (§6.5)

    async def get_mappings(self, signature: str) -> list[dict]:
        """ВСЕ запомненные трактовки этого формата — по одной на лист.

        Возвращается список, а не одна запись: у мультилистового прайса каждый лист
        разбирается своими колонками, и отдать только один значило бы сузить работу
        агента до него одного.
        """
        if not signature:
            return []
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT sheet, supplier, mapping, updated_at, uses FROM price_mappings "
                "WHERE signature = ? ORDER BY sheet", (signature,))
            rows = await cur.fetchall()
            if rows:
                await db.execute("UPDATE price_mappings SET uses = uses + 1 "
                                 "WHERE signature = ?", (signature,))
                await db.commit()
        return [{"sheet": r[0], "supplier": r[1], "mapping": json.loads(r[2]),
                 "updated_at": r[3], "uses": r[4]} for r in rows]

    async def list_mappings(self) -> list[dict]:
        """Все запомненные форматы, свежие сверху — для команды /mappings."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT signature, sheet, supplier, mapping, updated_at, uses "
                "FROM price_mappings ORDER BY updated_at DESC, sheet")
            rows = await cur.fetchall()
        return [{"signature": r[0], "sheet": r[1], "supplier": r[2],
                 "mapping": json.loads(r[3]), "updated_at": r[4], "uses": r[5]} for r in rows]

    async def forget_mapping(self, signature: str, sheet: str = "") -> bool:
        """Забыть трактовку одного листа — остальные листы того же файла остаются."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM price_mappings WHERE signature = ? AND sheet = ?",
                (signature, sheet or ""))
            await db.commit()
            return cur.rowcount > 0

    async def save_mapping(self, signature: str, supplier: str | None, mapping: dict,
                           sheet: str = "") -> None:
        """Upsert по (сигнатура, лист): трактовка одного листа не трогает остальные."""
        if not signature:
            return
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO price_mappings (signature, sheet, supplier, mapping, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(signature, sheet) DO UPDATE SET "
                "supplier = excluded.supplier, mapping = excluded.mapping, "
                "updated_at = excluded.updated_at",
                (signature, sheet or "", supplier, json.dumps(mapping, ensure_ascii=False),
                 _now()))
            await db.commit()

    # ----------------------------------------------- прогон прайса по маркам (§9.6)

    async def start_run(self, user_id: int, supplier: str | None, price_doc: str | None,
                        trademarks: list[dict]) -> None:
        """Новый прогон заменяет прежний: один прайс у админа в работе за раз."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO price_run (user_id, supplier, price_doc, planned, done, "
                "started_at) VALUES (?, ?, ?, ?, '[]', ?) ON CONFLICT(user_id) DO UPDATE SET "
                "supplier = excluded.supplier, price_doc = excluded.price_doc, "
                "planned = excluded.planned, done = '[]', started_at = excluded.started_at",
                (user_id, supplier, price_doc,
                 json.dumps(trademarks, ensure_ascii=False), _now()))
            await db.commit()

    async def get_run(self, user_id: int) -> dict | None:
        """{supplier, price_doc, planned, done, remaining} либо None."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT supplier, price_doc, planned, done FROM price_run WHERE user_id = ?",
                (user_id,))
            row = await cur.fetchone()
        if not row:
            return None
        planned, done = json.loads(row[2]), set(json.loads(row[3]))
        return {"supplier": row[0], "price_doc": row[1], "planned": planned, "done": done,
                "remaining": [t for t in planned if t.get("code") not in done]}

    async def mark_tm_done(self, user_id: int, tm_code: str) -> dict | None:
        """Отметить марку обработанной; возвращает обновлённое состояние прогона."""
        run = await self.get_run(user_id)
        if run is None:
            return None
        done = sorted(run["done"] | {tm_code})
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("UPDATE price_run SET done = ? WHERE user_id = ?",
                             (json.dumps(done, ensure_ascii=False), user_id))
            await db.commit()
        return await self.get_run(user_id)

    async def clear_run(self, user_id: int) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM price_run WHERE user_id = ?", (user_id,))
            await db.commit()

    # -------------------------------------------------- категории товаров (§6.8)

    async def list_scope(self) -> list[dict]:
        """Категории, которые анализируем. Пустой список = ограничений нет."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT category, category_norm, added_at FROM product_scope ORDER BY category")
            return [{"category": r[0], "norm": r[1], "added_at": r[2]}
                    for r in await cur.fetchall()]

    async def add_scope(self, categories: list[str]) -> list[str]:
        """Добавить категории; возвращает те, которых ещё не было."""
        from src.price_tool.scope import normalize

        added = []
        async with aiosqlite.connect(self._db_path) as db:
            for raw in categories:
                name = (raw or "").strip()
                norm = normalize(name)
                if not norm:
                    continue
                cur = await db.execute(
                    "INSERT OR IGNORE INTO product_scope (category_norm, category, added_at) "
                    "VALUES (?, ?, ?)", (norm, name, _now()))
                if cur.rowcount:
                    added.append(name)
            await db.commit()
        return added

    async def remove_scope(self, category: str) -> bool:
        from src.price_tool.scope import normalize

        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("DELETE FROM product_scope WHERE category_norm = ?",
                                   (normalize(category),))
            await db.commit()
            return cur.rowcount > 0

    async def reject(self, user_id: int, proposal_id: int) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE pending_proposal SET status = 'rejected' "
                "WHERE proposal_id = ? AND user_id = ? AND status = 'pending'",
                (proposal_id, user_id))
            await db.commit()
            return cur.rowcount > 0
