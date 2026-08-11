"""SQLite: состояние диалога и подготовленные к записи предложения по ценам.

`pending_proposal` — ключевой элемент безопасности (§10 спеки): агент формирует payload
`set-prices`, код его сохраняет, а отправляет в 1С **только** обработчик кнопки
подтверждения. Модель не может ни записать цены, ни изменить payload после подтверждения:
на кнопку уходит ровно то, что админ видел в предложении.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
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
"""

DIALOG_TTL_MINUTES = 60


@dataclass(frozen=True)
class Proposal:
    proposal_id: int
    user_id: int
    payload: list[dict]
    summary: str
    item_count: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PricingStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
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

    async def save_proposal(self, user_id: int, payload: list[dict], summary: str) -> int:
        """Новое предложение заменяет предыдущее неподтверждённое."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("UPDATE pending_proposal SET status = 'rejected' "
                             "WHERE user_id = ? AND status = 'pending'", (user_id,))
            cur = await db.execute(
                "INSERT INTO pending_proposal (user_id, payload, summary, item_count, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, json.dumps(payload, ensure_ascii=False), summary, len(payload), _now()))
            await db.commit()
            return cur.lastrowid

    async def get_pending(self, user_id: int) -> Proposal | None:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT proposal_id, payload, summary, item_count FROM pending_proposal "
                "WHERE user_id = ? AND status = 'pending' ORDER BY proposal_id DESC LIMIT 1",
                (user_id,))
            row = await cur.fetchone()
        if not row:
            return None
        return Proposal(proposal_id=row[0], user_id=user_id, payload=json.loads(row[1]),
                        summary=row[2], item_count=row[3])

    async def take_pending(self, user_id: int, proposal_id: int) -> Proposal | None:
        """Атомарно забирает предложение в работу: повторное нажатие кнопки не сработает."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE pending_proposal SET status = 'applied' "
                "WHERE proposal_id = ? AND user_id = ? AND status = 'pending' "
                "RETURNING payload, summary, item_count",
                (proposal_id, user_id))
            row = await cur.fetchone()
            await db.commit()
        if not row:
            return None
        return Proposal(proposal_id=proposal_id, user_id=user_id, payload=json.loads(row[0]),
                        summary=row[1], item_count=row[2])

    async def reject(self, user_id: int, proposal_id: int) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE pending_proposal SET status = 'rejected' "
                "WHERE proposal_id = ? AND user_id = ? AND status = 'pending'",
                (proposal_id, user_id))
            await db.commit()
            return cur.rowcount > 0
