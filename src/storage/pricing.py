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
    notes      TEXT NOT NULL DEFAULT '[]',  -- замечания по прайсу в целом: показать в конце
    price_date TEXT,                -- дата прайса: по ней считается устаревание задач
    stage      TEXT,                -- очередь коллекций внутри текущей марки (§9.6.2)
    started_at TEXT NOT NULL
);
-- Отложенные задачи: к чему решили вернуться позже (§9.7). Переживают конец прогона и
-- перезапуск бота, поэтому отдельно от price_run, который в конце очищается.
CREATE TABLE IF NOT EXISTS deferred_tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    supplier       TEXT,
    supplier_norm  TEXT,            -- сверка «прайс того же поставщика»
    price_doc      TEXT,
    price_date     TEXT,            -- дата прайса: ТОЛЬКО по ней считается устаревание
    signature      TEXT,            -- сигнатура формата (§6.5.2) — второй признак того же прайса
    file_path      TEXT,            -- сохранённый прайс, если сохранить удалось
    tm_code        TEXT NOT NULL,
    tm_name        TEXT NOT NULL DEFAULT '',
    collection_ref TEXT NOT NULL DEFAULT '',
    collection     TEXT NOT NULL DEFAULT '',
    reason         TEXT,
    stale          INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    UNIQUE (user_id, tm_code, collection_ref)
);
CREATE INDEX IF NOT EXISTS ix_deferred_user ON deferred_tasks (user_id, created_at);
-- Раскладка разобранного прайса: где какой бренд (§6.9). Ключ — ХЕШ СОДЕРЖИМОГО: только
-- он означает «тот же самый файл». Сигнатура (§6.5.2) для этого не годится — она считается
-- по скелету (листы + шапка, без данных), и прайс того же поставщика за следующий месяц
-- имеет ту же сигнатуру, но другие строки.
-- Чистится, когда от того же поставщика пришёл прайс НОВЕЕ.
CREATE TABLE IF NOT EXISTS price_layout (
    file_hash     TEXT PRIMARY KEY,
    supplier      TEXT,
    supplier_norm TEXT,
    price_doc     TEXT,
    price_date    TEXT,
    sections      TEXT NOT NULL,     -- JSON: [{code, name, first_row, last_row}]
    updated_at    TEXT NOT NULL
);
-- Решение админа по крупному прайсу (§6.10): разбирать или он сделает вручную.
-- Ключ — хеш содержимого: повторная присылка ТОГО ЖЕ файла вопрос не повторяет, а
-- прайс за следующий месяц спросит заново — решение принималось не про него.
CREATE TABLE IF NOT EXISTS price_decisions (
    file_hash     TEXT PRIMARY KEY,
    supplier      TEXT,
    price_doc     TEXT,
    price_date    TEXT,
    decision      TEXT NOT NULL,     -- process | manual
    rows          INTEGER,
    reason        TEXT,
    decided_at    TEXT NOT NULL
);
-- Общие настройки бота: сейчас режим работы (§5.2), дальше — прочие одиночные значения.
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
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

    @property
    def db_path(self) -> Path:
        """Путь к базе — рядом с ней лежат сохранённые прайсы (§9.7)."""
        return self._db_path

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            # база уже могла быть создана до появления дайджеста — дописываем колонку
            cur = await db.execute("PRAGMA table_info(pending_proposal)")
            columns = {row[1] for row in await cur.fetchall()}
            if "digest" not in columns:
                await db.execute("ALTER TABLE pending_proposal ADD COLUMN digest TEXT")
            cur = await db.execute("PRAGMA table_info(price_run)")
            run_columns = {row[1] for row in await cur.fetchall()}
            if "notes" not in run_columns:
                await db.execute("ALTER TABLE price_run ADD COLUMN notes TEXT "
                                 "NOT NULL DEFAULT '[]'")
            if "stage" not in run_columns:
                await db.execute("ALTER TABLE price_run ADD COLUMN stage TEXT")
            if "price_date" not in run_columns:
                await db.execute("ALTER TABLE price_run ADD COLUMN price_date TEXT")
            await self._migrate_mappings(db)
            for table in ("price_layout", "price_decisions"):
                cur = await db.execute(f"PRAGMA table_info({table})")
                cols = {row[1] for row in await cur.fetchall()}
                if cols and "file_hash" not in cols:
                    # ключом была сигнатура формата — это неверная идентификация файла;
                    # содержимое кэша восстановится само при следующем разборе
                    await db.execute(f"DROP TABLE {table}")
                    await db.executescript(_SCHEMA)
                    logger.info("Кеш %s пересоздан: ключ сменился на хеш содержимого", table)
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

    async def mark_rejected(self, proposal_id: int) -> None:
        """Предложение снято с рассмотрения: «Пропустить»/«Отложить» (§9.7)."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("UPDATE pending_proposal SET status = 'rejected' "
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
                        trademarks: list[dict], price_date: str | None = None) -> None:
        """Новый прогон заменяет прежний: один прайс у админа в работе за раз."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO price_run (user_id, supplier, price_doc, price_date, planned, "
                "done, stage, started_at) VALUES (?, ?, ?, ?, ?, '[]', NULL, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "supplier = excluded.supplier, price_doc = excluded.price_doc, "
                "price_date = excluded.price_date, planned = excluded.planned, "
                "done = '[]', stage = NULL, started_at = excluded.started_at",
                (user_id, supplier, price_doc, price_date or None,
                 json.dumps(trademarks, ensure_ascii=False), _now()))
            await db.commit()

    async def get_run(self, user_id: int) -> dict | None:
        """{supplier, price_doc, planned, done, remaining} либо None."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT supplier, price_doc, planned, done, notes, stage, price_date "
                "FROM price_run WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
        if not row:
            return None
        planned, done = json.loads(row[2]), set(json.loads(row[3]))
        stage = json.loads(row[5]) if row[5] else None
        if stage is not None:
            stage_done = set(stage.get("done") or [])
            stage["remaining"] = [c for c in stage.get("planned") or []
                                  if c.get("ref") not in stage_done]
        return {"supplier": row[0], "price_doc": row[1], "price_date": row[6],
                "planned": planned, "done": done, "notes": json.loads(row[4] or "[]"),
                "stage": stage,
                "remaining": [t for t in planned if t.get("code") not in done]}

    # --------------------------------------- очередь коллекций внутри марки (§9.6.2)

    async def start_stage(self, user_id: int, tm_code: str, tm_name: str,
                          collections: list[dict]) -> dict | None:
        """Крупная марка разбирается по коллекциям — очередь второго уровня."""
        stage = {"tm_code": tm_code, "tm_name": tm_name,
                 "planned": [{"ref": c["ref"], "name": c.get("name") or c["ref"]}
                             for c in collections if c.get("ref")],
                 "done": []}
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("UPDATE price_run SET stage = ? WHERE user_id = ?",
                             (json.dumps(stage, ensure_ascii=False), user_id))
            await db.commit()
        return await self.get_run(user_id)

    async def mark_collection_done(self, user_id: int, collection_ref: str) -> dict | None:
        """Закрыть коллекцию; когда очередь опустела — закрыть и саму марку."""
        run = await self.get_run(user_id)
        stage = (run or {}).get("stage")
        if not stage:
            return run
        done = sorted(set(stage.get("done") or []) | {collection_ref})
        rest = [c for c in stage.get("planned") or [] if c.get("ref") not in set(done)]
        if rest:
            stage["done"] = done
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("UPDATE price_run SET stage = ? WHERE user_id = ?",
                                 (json.dumps(stage, ensure_ascii=False), user_id))
                await db.commit()
            return await self.get_run(user_id)
        # коллекции кончились — марка пройдена, второй уровень сворачивается
        await self.clear_stage(user_id)
        return await self.mark_tm_done(user_id, stage["tm_code"])

    async def clear_stage(self, user_id: int) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("UPDATE price_run SET stage = NULL WHERE user_id = ?",
                             (user_id,))
            await db.commit()

    async def add_run_notes(self, user_id: int, notes: list[str]) -> int:
        """Замечания по прайсу в целом — копятся и показываются один раз, в конце (§9.6).

        Повторы отсеиваются: одно и то же наблюдение («бренд не в выгрузке») агент делает
        при разборе каждой марки, а админу оно нужно однажды.
        """
        run = await self.get_run(user_id)
        if run is None:
            return 0
        merged = list(run["notes"])
        seen = {n.strip().lower() for n in merged}
        for note in notes:
            text = (note or "").strip()
            if text and text.lower() not in seen:
                seen.add(text.lower())
                merged.append(text)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("UPDATE price_run SET notes = ? WHERE user_id = ?",
                             (json.dumps(merged, ensure_ascii=False), user_id))
            await db.commit()
        return len(merged) - len(run["notes"])

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

    # -------------------------------------------------- настройки (§5.2)

    async def get_setting(self, key: str, default: str = "") -> str:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
            row = await cur.fetchone()
        return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at", (key, value, _now()))
            await db.commit()

    # ------------------------------- решение по крупному прайсу (§6.10)

    async def save_decision(self, file_hash: str, decision: str, supplier: str | None,
                            price_doc: str | None, price_date: str | None,
                            rows: int | None = None, reason: str | None = None) -> None:
        if not file_hash or decision not in ("process", "manual"):
            return
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO price_decisions (file_hash, supplier, price_doc, price_date, "
                "decision, rows, reason, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(file_hash) DO UPDATE SET decision = excluded.decision, "
                "reason = excluded.reason, decided_at = excluded.decided_at",
                (file_hash, supplier, price_doc, (price_date or "")[:10], decision,
                 rows, reason, _now()))
            await db.commit()

    async def get_decision(self, file_hash: str) -> dict | None:
        if not file_hash:
            return None
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT decision, supplier, price_doc, price_date, rows, reason, decided_at "
                "FROM price_decisions WHERE file_hash = ?", (file_hash,))
            row = await cur.fetchone()
        keys = ("decision", "supplier", "price_doc", "price_date", "rows", "reason",
                "decided_at")
        return dict(zip(keys, row)) if row else None

    async def list_manual(self) -> list[dict]:
        """Прайсы, которые админ решил обработать вручную — их показываем в отчётах."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT supplier, price_doc, price_date, rows, decided_at "
                "FROM price_decisions WHERE decision = 'manual' ORDER BY decided_at DESC")
            keys = ("supplier", "price_doc", "price_date", "rows", "decided_at")
            return [dict(zip(keys, r)) for r in await cur.fetchall()]

    # ------------------------------------------- раскладка прайса (§6.9)

    async def save_layout(self, file_hash: str, supplier: str | None, price_doc: str | None,
                          price_date: str | None, sections: list[dict]) -> None:
        """Запомнить, где какой бренд в этом файле."""
        from src.price_tool.scope import normalize

        if not file_hash or not sections:
            return
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO price_layout (file_hash, supplier, supplier_norm, price_doc, "
                "price_date, sections, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(file_hash) DO UPDATE SET supplier = excluded.supplier, "
                "supplier_norm = excluded.supplier_norm, price_doc = excluded.price_doc, "
                "price_date = excluded.price_date, sections = excluded.sections, "
                "updated_at = excluded.updated_at",
                (file_hash, supplier, normalize(supplier), price_doc, (price_date or "")[:10],
                 json.dumps(sections, ensure_ascii=False), _now()))
            await db.commit()

    async def get_layout(self, file_hash: str) -> dict | None:
        if not file_hash:
            return None
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT supplier, price_doc, price_date, sections, updated_at "
                "FROM price_layout WHERE file_hash = ?", (file_hash,))
            row = await cur.fetchone()
        if not row:
            return None
        return {"supplier": row[0], "price_doc": row[1], "price_date": row[2],
                "sections": json.loads(row[3]), "updated_at": row[4]}

    async def drop_old_layouts(self, supplier: str | None, price_date: str | None) -> int:
        """Пришёл прайс НОВЕЕ от того же поставщика — прежние раскладки устарели.

        Сравниваются только даты: тот же прайс, присланный заново, свою раскладку
        сохраняет — ради этого всё и заводилось.
        """
        from src.price_tool.scope import normalize

        day = (price_date or "")[:10]
        norm = normalize(supplier)
        if len(day) != 10 or not norm:
            return 0
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM price_layout WHERE supplier_norm = ? "
                "AND length(price_date) >= 10 AND price_date < ?", (norm, day))
            await db.commit()
            return cur.rowcount

    # ------------------------------------------------ отложенные задачи (§9.7)

    _DEFERRED_FIELDS = ("supplier", "supplier_norm", "price_doc", "price_date", "signature",
                        "file_path", "tm_code", "tm_name", "collection_ref", "collection",
                        "reason")

    async def defer_task(self, user_id: int, task: dict) -> bool:
        """Отложить марку или коллекцию. Повтор того же объекта не плодит записей."""
        from src.price_tool.scope import normalize

        row = {f: task.get(f) or "" for f in self._DEFERRED_FIELDS}
        if not row["tm_code"]:
            return False
        row["supplier_norm"] = normalize(task.get("supplier"))
        row["file_path"] = task.get("file_path") or None
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                f"INSERT OR IGNORE INTO deferred_tasks (user_id, "
                f"{', '.join(self._DEFERRED_FIELDS)}, created_at) "
                f"VALUES ({', '.join('?' * (len(self._DEFERRED_FIELDS) + 2))})",
                (user_id, *(row[f] for f in self._DEFERRED_FIELDS), _now()))
            await db.commit()
            return cur.rowcount > 0

    async def list_deferred(self, user_id: int) -> list[dict]:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, tm_name, tm_code, collection, collection_ref, supplier, "
                "price_doc, price_date, file_path, stale, reason FROM deferred_tasks "
                "WHERE user_id = ? ORDER BY id", (user_id,))
            rows = await cur.fetchall()
        keys = ("id", "tm_name", "tm_code", "collection", "collection_ref", "supplier",
                "price_doc", "price_date", "file_path", "stale", "reason")
        return [dict(zip(keys, r)) for r in rows]

    async def _drop_deferred(self, user_id: int, where: str, params: tuple) -> list[str]:
        """Удаляет записи и возвращает файлы, на которые больше никто не ссылается."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                f"SELECT file_path FROM deferred_tasks WHERE user_id = ? AND {where}",
                (user_id, *params))
            touched = {r[0] for r in await cur.fetchall() if r[0]}
            await db.execute(
                f"DELETE FROM deferred_tasks WHERE user_id = ? AND {where}",
                (user_id, *params))
            await db.commit()
            if not touched:
                return []
            cur = await db.execute(
                "SELECT DISTINCT file_path FROM deferred_tasks WHERE file_path IS NOT NULL")
            still_used = {r[0] for r in await cur.fetchall()}
        return sorted(touched - still_used)

    async def forget_deferred(self, user_id: int, task_id: int) -> list[str]:
        return await self._drop_deferred(user_id, "id = ?", (task_id,))

    async def clear_deferred(self, user_id: int, stale_only: bool = False) -> list[str]:
        return await self._drop_deferred(user_id, "stale = 1" if stale_only else "1 = 1", ())

    async def drop_deferred_for(self, user_id: int, tm_code: str,
                                collection_ref: str = "") -> list[str]:
        """Объект реально обработан — держать по нему отложенную задачу незачем."""
        return await self._drop_deferred(
            user_id, "tm_code = ? AND collection_ref = ?", (tm_code, collection_ref or ""))

    async def mark_stale(self, user_id: int, supplier: str | None, signature: str | None,
                         price_date: str | None) -> list[dict]:
        """Пометить задачи, которые перекрыты БОЛЕЕ СВЕЖИМ прайсом того же поставщика.

        Сравниваются ТОЛЬКО даты прайсов. Тот же прайс, присланный заново, — обычный
        способ вернуться к отложенному, и ронять из-за него задачу нельзя; переэкспорт с
        той же датой тоже не устаревание, хотя содержимое файла и отличается. Нет даты у
        нового прайса или у задачи — сравнивать не с чем, молчим.
        """
        from src.price_tool.scope import normalize

        day = (price_date or "")[:10]
        if len(day) != 10:
            return []
        norm = normalize(supplier)
        if not norm and not signature:
            return []
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, tm_name, tm_code, collection, price_date FROM deferred_tasks "
                "WHERE user_id = ? AND stale = 0 AND length(price_date) >= 10 "
                "AND substr(price_date, 1, 10) < ? "
                "AND ((? <> '' AND supplier_norm = ?) OR (? <> '' AND signature = ?))",
                (user_id, day, norm, norm, signature or "", signature or ""))
            rows = await cur.fetchall()
            if rows:
                await db.executemany("UPDATE deferred_tasks SET stale = 1 WHERE id = ?",
                                     [(r[0],) for r in rows])
                await db.commit()
        keys = ("id", "tm_name", "tm_code", "collection", "price_date")
        return [dict(zip(keys, r)) for r in rows]

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
