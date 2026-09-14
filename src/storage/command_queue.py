"""Очередь команд визуалов (§7, §9 specs/agent-workflow-model.md).

Визуал кладёт команду сюда, агент забирает её своим циклом. Очередь в БАЗЕ, а не в памяти:
команда, присланная перед остановкой процесса, должна дождаться его подъёма — ровно этим
опрос визуалов и лучше слушателя, у которого такая команда пропала бы.

**Замещение — по ОБЪЕКТУ, вид в ключ не входит.** Более новая команда по той же задаче
(или тому же прайсу) перезаписывает лежащую в очереди, какого бы вида та ни была: в модель
приезжает последнее решение админа, а не цепочка промежуточных.

**Полезная нагрузка при этом ОБЪЕДИНЯЕТСЯ.** Админ правит описание задачи и тут же жмёт
«Выполнить»: замещающая команда — «выполнить», но текст правки обязан доехать вместе с ней,
иначе агент получит старое задание.

**Часы источника.** Порядок решает время создания на стороне визуала, но у 1С свой сервер и
свои часы. `put` принимает измеренное смещение и кладёт рядом с сырой меткой приведённую к
нашим часам (`sort_at`) — сортировка идёт по ней. У Telegram смещение нулевое по построению:
метку ставит тот же процесс.

**Взятие помечает, а не удаляет.** Между «забрал» и «выполнил» процесс может умереть, и
тогда по базе видно, на чём он встал. Удаляет команду тот, кто её обработал.
"""
from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from src.model.commands import Command, CommandKind, sort_time

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
    -- То же время, ПРИВЕДЁННОЕ К НАШИМ ЧАСАМ (`commands.sort_time`). Сортируем по нему:
    -- часы 1С могут быть сбиты, и тогда сырая метка дала бы ей вечное преимущество.
    sort_at    TEXT NOT NULL,
    skew_note  TEXT NOT NULL DEFAULT '',   -- почему метке не поверили, если не поверили
    queued_at  TEXT NOT NULL,
    taken_at   TEXT                        -- NULL = ждёт; иначе агент её уже забрал
);
CREATE INDEX IF NOT EXISTS ix_queue_pending ON command_queue (taken_at, sort_at);
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
            # База могла быть создана до появления поправки на часы — дописываем колонки.
            cur = await db.execute("PRAGMA table_info(command_queue)")
            have = {row[1] for row in await cur.fetchall()}
            if have and "sort_at" not in have:
                await db.execute("ALTER TABLE command_queue ADD COLUMN sort_at TEXT "
                                 "NOT NULL DEFAULT ''")
                await db.execute("UPDATE command_queue SET sort_at = created_at "
                                 "WHERE sort_at = ''")
            if have and "skew_note" not in have:
                await db.execute("ALTER TABLE command_queue ADD COLUMN skew_note TEXT "
                                 "NOT NULL DEFAULT ''")
            await db.commit()

    async def put(self, command: Command, offset_seconds: float = 0.0,
                  agent_now=None) -> Command:
        """Положить команду в очередь, заместив лежащую того же вида по тому же объекту.

        `offset_seconds` — насколько часы источника уходят вперёд относительно наших.
        Для Telegram всегда ноль: метку ставит тот же процесс.
        """
        key = command.coalesce_key()
        payload = json.dumps(command.payload or {}, ensure_ascii=False)
        command.sort_at, note = sort_time(command.created_at, offset_seconds, agent_now)

        async with aiosqlite.connect(self._db_path) as db:
            if key is not None:
                # Объект сравнивается СВОЕГО РОДА. Иначе команда по прайсу нашла бы строки
                # его задач: у них тоже проставлен `price_id`, — и «пересобрать задачи»
                # молча съело бы правку описания.
                if command.task_id is not None:
                    where, target = "task_id IS ?", command.task_id
                else:
                    where, target = "price_id IS ? AND task_id IS NULL", command.price_id
                cur = await db.execute(
                    f"SELECT id, payload FROM command_queue WHERE {where} "
                    "AND taken_at IS NULL", (target,))
                twin = await cur.fetchone()
                if twin:
                    # ПОЛЕЗНАЯ НАГРУЗКА ОБЪЕДИНЯЕТСЯ, а не затирается: админ правит описание
                    # и тут же жмёт «Выполнить». Замещающая команда — «выполнить», но текст
                    # правки обязан доехать вместе с ней, иначе агент получит старое задание.
                    # Свои значения важнее: они новее.
                    try:
                        merged = json.loads(twin[1] or "{}")
                    except (ValueError, TypeError):
                        merged = {}
                    merged.update(command.payload or {})
                    command.payload = merged

                    await db.execute(
                        "UPDATE command_queue SET kind = ?, payload = ?, created_at = ?, "
                        "sort_at = ?, skew_note = ?, source = ?, actor = ?, "
                        "price_id = ?, queued_at = ? WHERE id = ?",
                        (command.kind.value, json.dumps(merged, ensure_ascii=False),
                         command.created_at, command.sort_at, note, command.source,
                         command.actor, command.price_id, _now(), twin[0]))
                    await db.commit()
                    command.id = twin[0]
                    return command

            cur = await db.execute(
                "INSERT INTO command_queue (kind, source, actor, price_id, task_id, "
                "payload, created_at, sort_at, skew_note, queued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (command.kind.value, command.source, command.actor, command.price_id,
                 command.task_id, payload, command.created_at, command.sort_at,
                 note, _now()))
            await db.commit()
            command.id = cur.lastrowid
            return command

    async def pending(self) -> list[Command]:
        """Что ждёт разбора, в порядке создания на стороне визуала."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, kind, source, actor, price_id, task_id, payload, created_at, "
                "sort_at FROM command_queue WHERE taken_at IS NULL ORDER BY sort_at, id")
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
                "SELECT id, kind, source, actor, price_id, task_id, payload, created_at, "
                "sort_at FROM command_queue WHERE taken_at IS NOT NULL ORDER BY sort_at, id")
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
    cid, kind, source, actor, price_id, task_id, payload, created_at, sort_at = row
    try:
        data = json.loads(payload or "{}")
    except (ValueError, TypeError):
        data = {}
    return Command(kind=CommandKind(kind), source=source, actor=actor,
                   price_id=price_id, task_id=task_id, payload=data,
                   created_at=created_at, sort_at=sort_at or created_at, id=cid)
