"""Очередь команд визуалов (§7, §9 specs/agent-workflow-model.md).

Визуал кладёт команду сюда, агент забирает её своим циклом. Очередь в БАЗЕ, а не в памяти:
команда, присланная перед остановкой процесса, должна дождаться его подъёма — ровно этим
опрос визуалов и лучше слушателя, у которого такая команда пропала бы.

**Схлопывание.** Повторная смена статуса того же объекта перезаписывает лежащую в очереди
команду, а не добавляет вторую: в модель приезжает последнее решение админа, а не цепочка
промежуточных. Схлопываются только присваивания (`COALESCING` в `src/model/commands.py`).

**Взятие помечает, а не удаляет.** Между «забрал» и «выполнил» процесс может умереть, и
тогда по базе видно, на чём он встал. Удаляет команду тот, кто её обработал.
"""
from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from src.model.commands import Command, CommandKind

_SCHEMA = """
CREATE TABLE IF NOT EXISTS command_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '',   -- какой провайдер принёс: telegram | 1c
    actor      TEXT NOT NULL DEFAULT '',   -- кто из админов: захват принадлежит ему
    price_id   INTEGER,
    task_id    INTEGER,
    payload    TEXT NOT NULL DEFAULT '{}',
    -- Время создания НА СТОРОНЕ ВИЗУАЛА, не попадания в очередь: по нему решается,
    -- кто первый (§7). Порядок обхода провайдеров сделал бы один визуал главнее другого.
    created_at TEXT NOT NULL,
    queued_at  TEXT NOT NULL,
    taken_at   TEXT                        -- NULL = ждёт; иначе агент её уже забрал
);
CREATE INDEX IF NOT EXISTS ix_queue_pending ON command_queue (taken_at, created_at);
"""


def _now() -> str:
    from src.model.commands import now
    return now()


class CommandQueue:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def put(self, command: Command) -> Command:
        """Положить команду в очередь. Присваивания схлопываются с уже лежащими."""
        key = command.coalesce_key()
        payload = json.dumps(command.payload or {}, ensure_ascii=False)

        async with aiosqlite.connect(self._db_path) as db:
            if key is not None:
                target_col = "task_id" if command.task_id is not None else "price_id"
                target = command.task_id if command.task_id is not None else command.price_id
                cur = await db.execute(
                    f"SELECT id FROM command_queue WHERE kind = ? AND {target_col} IS ? "
                    "AND taken_at IS NULL", (command.kind.value, target))
                twin = await cur.fetchone()
                if twin:
                    # Перезаписываем вместе с временем создания: в модель должно приехать
                    # последнее решение, и «первым» оно теперь считается по нему же.
                    await db.execute(
                        "UPDATE command_queue SET payload = ?, created_at = ?, "
                        "source = ?, actor = ?, queued_at = ? WHERE id = ?",
                        (payload, command.created_at, command.source, command.actor,
                         _now(), twin[0]))
                    await db.commit()
                    command.id = twin[0]
                    return command

            cur = await db.execute(
                "INSERT INTO command_queue (kind, source, actor, price_id, task_id, "
                "payload, created_at, queued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (command.kind.value, command.source, command.actor, command.price_id,
                 command.task_id, payload, command.created_at, _now()))
            await db.commit()
            command.id = cur.lastrowid
            return command

    async def pending(self) -> list[Command]:
        """Что ждёт разбора, в порядке создания на стороне визуала."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, kind, source, actor, price_id, task_id, payload, created_at "
                "FROM command_queue WHERE taken_at IS NULL ORDER BY created_at, id")
            return [_from_row(row) for row in await cur.fetchall()]

    async def take(self, limit: int = 100) -> list[Command]:
        """Забрать пачку и пометить взятой.

        Помечаем, а не удаляем: между «забрал» и «выполнил» процесс может умереть, и тогда
        по базе видно, на чём он встал. Удалит команду тот, кто её обработает.
        """
        batch = (await self.pending())[:limit]
        if not batch:
            return []
        stamp = _now()
        async with aiosqlite.connect(self._db_path) as db:
            await db.executemany("UPDATE command_queue SET taken_at = ? WHERE id = ?",
                                 [(stamp, c.id) for c in batch])
            await db.commit()
        return batch

    async def done(self, command_id: int) -> bool:
        """Команда обработана — убрать из очереди."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("DELETE FROM command_queue WHERE id = ?", (command_id,))
            await db.commit()
            return cur.rowcount > 0

    async def release(self, command_id: int) -> bool:
        """Вернуть взятую команду в очередь: обработать не вышло, пусть ждёт следующего круга."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE command_queue SET taken_at = NULL WHERE id = ?", (command_id,))
            await db.commit()
            return cur.rowcount > 0

    async def taken(self) -> list[Command]:
        """Взятые, но не завершённые — диагностика зависшего разбора."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, kind, source, actor, price_id, task_id, payload, created_at "
                "FROM command_queue WHERE taken_at IS NOT NULL ORDER BY created_at, id")
            return [_from_row(row) for row in await cur.fetchall()]

    async def requeue_stale(self) -> int:
        """Вернуть в очередь всё взятое. Зовётся при СТАРТЕ процесса.

        Взятая команда без завершения означает одно: процесс умер, не доработав. Живых
        взятых команд в момент старта быть не может — агент один (§6), — поэтому их можно
        безопасно вернуть, а не гадать, кто их держит.
        """
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE command_queue SET taken_at = NULL WHERE taken_at IS NOT NULL")
            await db.commit()
            return cur.rowcount

    async def clear(self) -> int:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("DELETE FROM command_queue")
            await db.commit()
            return cur.rowcount


def _from_row(row) -> Command:
    cid, kind, source, actor, price_id, task_id, payload, created_at = row
    try:
        data = json.loads(payload or "{}")
    except (ValueError, TypeError):
        data = {}
    return Command(kind=CommandKind(kind), source=source, actor=actor,
                   price_id=price_id, task_id=task_id, payload=data,
                   created_at=created_at, id=cid)
