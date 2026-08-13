"""SQLite: состояние диалога и подготовленные к записи предложения по ценам.

`pending_proposal` — ключевой элемент безопасности (§10 спеки): агент формирует payload
`set-prices`, код его сохраняет, а отправляет в 1С **только** обработчик кнопки
подтверждения. Модель не может ни записать цены, ни изменить payload после подтверждения:
на кнопку уходит ровно то, что админ видел в предложении.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

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
CREATE TABLE IF NOT EXISTS price_mappings (
    signature  TEXT PRIMARY KEY,        -- сигнатура структуры прайса (§6.5.2)
    supplier   TEXT,                    -- как назывался поставщик — для показа админу
    mapping    TEXT NOT NULL,           -- JSON: колонки цен, база, лист
    updated_at TEXT NOT NULL,
    uses       INTEGER NOT NULL DEFAULT 0
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
"""

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
            await db.commit()

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

    async def get_mapping(self, signature: str) -> dict | None:
        """Запомненная трактовка колонок для этого формата прайса."""
        if not signature:
            return None
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT supplier, mapping, updated_at, uses FROM price_mappings "
                "WHERE signature = ?", (signature,))
            row = await cur.fetchone()
            if row:
                await db.execute("UPDATE price_mappings SET uses = uses + 1 "
                                 "WHERE signature = ?", (signature,))
                await db.commit()
        if not row:
            return None
        return {"supplier": row[0], "mapping": json.loads(row[1]),
                "updated_at": row[2], "uses": row[3]}

    async def list_mappings(self) -> list[dict]:
        """Все запомненные форматы, свежие сверху — для команды /mappings."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT signature, supplier, mapping, updated_at, uses FROM price_mappings "
                "ORDER BY updated_at DESC")
            rows = await cur.fetchall()
        return [{"signature": r[0], "supplier": r[1], "mapping": json.loads(r[2]),
                 "updated_at": r[3], "uses": r[4]} for r in rows]

    async def forget_mapping(self, signature: str) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("DELETE FROM price_mappings WHERE signature = ?", (signature,))
            await db.commit()
            return cur.rowcount > 0

    async def save_mapping(self, signature: str, supplier: str | None, mapping: dict) -> None:
        """Upsert: ответ админа перекрывает прежнюю трактовку того же формата."""
        if not signature:
            return
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO price_mappings (signature, supplier, mapping, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(signature) DO UPDATE SET "
                "supplier = excluded.supplier, mapping = excluded.mapping, "
                "updated_at = excluded.updated_at",
                (signature, supplier, json.dumps(mapping, ensure_ascii=False), _now()))
            await db.commit()

    async def reject(self, user_id: int, proposal_id: int) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE pending_proposal SET status = 'rejected' "
                "WHERE proposal_id = ? AND user_id = ? AND status = 'pending'",
                (proposal_id, user_id))
            await db.commit()
            return cur.rowcount > 0
