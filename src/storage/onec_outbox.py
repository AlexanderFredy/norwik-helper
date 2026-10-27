"""Сообщения агента для формы 1С — очередь на доставку (решение админа 27.10.2026).

ЗАЧЕМ. Всё, что агент сообщает админу («прайс принят», «задачи пересобраны», отказы
команд), уходило только в Telegram. 27.10.2026 связь сервера с Telegram пропала на полдня,
и админ, работающий в форме 1С, остался без единого ответа агента. Теперь те же тексты
копятся здесь и доставляются в 1С провайдером (`OnecProvider._push_messages`).

В БАЗЕ, а не в памяти, по той же причине, что и связки команд: сеть до 1С тоже рвётся, а
перезапуск процесса между «сообщение родилось» и «доехало» не должен его терять. Строка
снимается ТОЛЬКО после успешного ответа 1С; повтор безвреден — 1С пишет по идентификатору.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS onec_outbox (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,              -- когда родилось, UTC ISO
    kind     TEXT NOT NULL DEFAULT '',   -- вид события модели
    text     TEXT NOT NULL,
    price_id INTEGER,
    task_id  INTEGER
);
"""

#: Предел очереди. Если 1С недоступна сутками, старое уже никому не нужно, а таблица не
#: должна расти без конца: оставляем самые свежие.
MAX_KEPT = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OnecOutbox:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def add(self, text: str, kind: str = "", price_id=None, task_id=None,
                  at: str | None = None) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT INTO onec_outbox (at, kind, text, price_id, task_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (at or _now(), kind or "", text, price_id, task_id))
            await db.execute(
                "DELETE FROM onec_outbox WHERE id NOT IN "
                "(SELECT id FROM onec_outbox ORDER BY id DESC LIMIT ?)", (MAX_KEPT,))
            await db.commit()

    async def pending(self, limit: int = 50) -> list[dict]:
        """Самые старые недоставленные — в порядке рождения."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, at, kind, text, price_id, task_id FROM onec_outbox "
                "ORDER BY id LIMIT ?", (int(limit),))
            rows = await cur.fetchall()
        return [{"id": str(r[0]), "at": r[1], "kind": r[2], "text": r[3],
                 "price_id": r[4] or 0, "task_id": r[5] or 0} for r in rows]

    async def forget(self, ids) -> None:
        ids = [int(i) for i in ids]
        if not ids:
            return
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                f"DELETE FROM onec_outbox WHERE id IN ({', '.join('?' * len(ids))})", ids)
            await db.commit()
