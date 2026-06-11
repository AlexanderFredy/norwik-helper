"""SQLite-хранилище whitelist пользователей бота."""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS allowed_users (
    telegram_id INTEGER PRIMARY KEY,
    name        TEXT,
    added_by    INTEGER NOT NULL,
    added_at    TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class AllowedUser:
    telegram_id: int
    name: str | None
    added_by: int
    added_at: str


class UserStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(_SCHEMA)
            await db.commit()

    async def is_allowed(self, telegram_id: int) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM allowed_users WHERE telegram_id = ?", (telegram_id,)
            )
            return await cursor.fetchone() is not None

    async def add(self, telegram_id: int, added_by: int, name: str | None = None) -> bool:
        """Возвращает False, если пользователь уже был в списке."""
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO allowed_users (telegram_id, name, added_by, added_at) "
                "VALUES (?, ?, ?, ?)",
                (telegram_id, name, added_by, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def remove(self, telegram_id: int) -> bool:
        """Возвращает False, если пользователя не было в списке."""
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "DELETE FROM allowed_users WHERE telegram_id = ?", (telegram_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def list_all(self) -> list[AllowedUser]:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT telegram_id, name, added_by, added_at FROM allowed_users "
                "ORDER BY added_at"
            )
            rows = await cursor.fetchall()
            return [AllowedUser(*row) for row in rows]
