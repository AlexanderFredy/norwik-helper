"""Связка «команда очереди → команда 1С» (specs/1c-model-form.md §3.2).

В БАЗЕ, а не в памяти провайдера — и это не украшение, а требование инварианта «колесико
ожидания обязано гаснуть по результату».

Разбор случая 24.09.2026. Админ перезапустил бота, пока команда «пересобрать задачи» была
в состоянии «принята». `requeue_stale` вернул её в очередь, новый процесс честно её
отработал и удалил из очереди, — но идентификатора команды 1С он уже не знал: карта жила
в памяти умершего процесса. `agent-commands-state` по ней не ушёл, и форма осталась
заперта со статусом «идёт пересборка» НАВСЕГДА: GET `agent-commands` отдаёт только
«ждёт», значит агент такую строку больше даже не видит, а `УбратьСтарыеКоманды` намеренно
не трогает незакрытые. Разблокировать можно было только руками, удалив строку регистра.

Поэтому связка ложится в базу СРАЗУ ПОСЛЕ постановки в очередь — и обязательно до того,
как в 1С уедет «принята», — а снимается только после успешного ответа 1С об исходе. Оба
края важны:

* запись раньше «принята» делает окно самоизлечимым: умри процесс в нём, команда в 1С
  осталась «ждёт» и приедет следующим опросом, а в очереди схлопнется с уже лежащей;
* снятие после успеха — иначе моргнувшая сеть съедает ответ молча, и колесико опять горит.

СПИСОК идентификаторов на одну команду очереди, а не одно значение: очередь схлопывает
команды по объекту, и закрывать надо все схлопнутые, иначе в форме останется гореть
колесико по команде, которой уже нет.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS onec_sent_commands (
    queue_id INTEGER NOT NULL,   -- id строки в command_queue
    external TEXT    NOT NULL,   -- идентификатор команды в регистре 1С
    noted_at TEXT    NOT NULL,
    PRIMARY KEY (queue_id, external)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SentCommands:
    """Что из очереди чем является в 1С. Переживает перезапуск процесса."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def remember(self, queue_id: int, external: str) -> None:
        """Запомнить, что команда очереди `queue_id` — это команда 1С `external`.

        Повтор безвреден: та же пара просто остаётся одной строкой.
        """
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO onec_sent_commands (queue_id, external, noted_at) "
                "VALUES (?, ?, ?)", (int(queue_id), str(external), _now()))
            await db.commit()

    async def all(self) -> dict[int, list[str]]:
        """Вся карта целиком. Читается один раз при подъёме провайдера."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT queue_id, external FROM onec_sent_commands ORDER BY noted_at, rowid")
            rows = await cur.fetchall()

        out: dict[int, list[str]] = {}
        for queue_id, external in rows:
            out.setdefault(int(queue_id), []).append(str(external))
        return out

    async def forget(self, queue_id: int) -> None:
        """Снять связку. Зовётся ТОЛЬКО после того, как 1С приняла исход."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM onec_sent_commands WHERE queue_id = ?",
                             (int(queue_id),))
            await db.commit()
