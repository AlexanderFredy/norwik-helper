"""Кому уходит еженедельный дайджест по фото (решение админа 30.09.2026).

**ОТДЕЛЬНЫЙ СПИСОК, А НЕ «ВСЕ ИЗ БЕЛОГО СПИСКА».** Доступ к боту и подписка на рассылку —
разные вещи: пользоваться ботом нужно всем менеджерам, а получать по понедельникам сводку
про фото — тем, кто этим занимается. **Админ здесь такой же участник**: нет его в списке —
дайджест ему не приходит, и это не недосмотр, а то же правило для всех.

**ПОДПИСАТЬ МОЖНО ТОЛЬКО ТОГО, У КОГО ЕСТЬ ДОСТУП К БОТУ.** Рассылка несёт наименования и
ссылки на карточки каталога; отправить это человеку, которому пользоваться ботом не
разрешали, значит выдать данные в обход того самого белого списка. Проверку делает
обработчик команды — у хранилища списка доступа нет и быть не должно.

**ПУСТОЙ СПИСОК — ЭТО «НЕ СЛАТЬ НИКОМУ»**, а не «слать всем». Обратное умолчание однажды
разошлёт каталог всем подряд после неудачной миграции.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_subscribers (
    telegram_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    added_by    INTEGER,
    added_at    TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Subscriber:
    telegram_id: int
    name: str
    added_by: int | None
    added_at: str


class PhotoSubscriberStore:
    def __init__(self, db_path) -> None:
        self._db_path = str(db_path)

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def add(self, telegram_id: int, name: str = "",
                  added_by: int | None = None) -> bool:
        """Подписать. False — уже был подписан.

        Повторная подписка ОБНОВЛЯЕТ имя, но не дату: дата отвечает на вопрос «с каких пор
        человек получает рассылку», и сбрасывать её переименованием незачем.
        """
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT 1 FROM photo_subscribers WHERE telegram_id = ?", (telegram_id,))
            existed = await cur.fetchone() is not None
            await db.execute(
                "INSERT INTO photo_subscribers (telegram_id, name, added_by, added_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(telegram_id) DO UPDATE SET name = excluded.name",
                (telegram_id, name, added_by,
                 datetime.now(timezone.utc).isoformat()))
            await db.commit()
        return not existed

    async def remove(self, telegram_id: int) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM photo_subscribers WHERE telegram_id = ?", (telegram_id,))
            await db.commit()
            return cur.rowcount > 0

    async def list_all(self) -> list[Subscriber]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT telegram_id, name, added_by, added_at FROM photo_subscribers"
                " ORDER BY added_at, telegram_id")
            return [Subscriber(**dict(r)) for r in await cur.fetchall()]

    async def has(self, telegram_id: int) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT 1 FROM photo_subscribers WHERE telegram_id = ?", (telegram_id,))
            return await cur.fetchone() is not None
