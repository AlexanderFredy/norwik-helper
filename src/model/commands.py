"""Команды визуалов и правила их разбора (§7 specs/agent-workflow-model.md).

Визуал кладёт команду в очередь, агент забирает её своим циклом и применяет. Здесь — чистая
часть: какие бывают команды, какие из них схлопываются и кто из пришедших за один цикл
работает первым. Ни базы, ни Telegram.

**Каждая команда несёт время своего создания НА СТОРОНЕ ВИЗУАЛА.** Не время попадания в
очередь и не порядок обхода провайдеров: иначе один визуал был бы всегда «главнее» другого,
а при сетевой задержке «первым» оказался бы тот, кто нажал позже.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CommandKind(str, Enum):
    SUBMIT_PRICE = "принять прайс"
    EXECUTE_TASK = "выполнить задачу"
    SET_TASK_STATUS = "сменить статус задачи"
    SET_PRICE_STATUS = "сменить статус прайса"
    EDIT_TASK_DESCRIPTION = "изменить описание задачи"
    DELETE_TASK = "удалить задачу"
    REBUILD_TASKS = "пересобрать задачи"
    DESTROY_PRICE = "уничтожить прайс"
    RELEASE_LOCK = "снять захват"


#: Команды, которые ЗАНИМАЮТ прайс: они меняют его состав или запускают работу по нему.
#: Только среди них действует «кто первый, тот и работает» (§7) — остальным отвечать
#: «прайс занят» было бы нелепо: смена статуса мгновенна, и две подряд это обычное дело.
EXCLUSIVE = frozenset({
    CommandKind.EXECUTE_TASK,
    CommandKind.REBUILD_TASKS,
    CommandKind.DESTROY_PRICE,
    CommandKind.DELETE_TASK,
})

#: Команды-присваивания: повторная по тому же объекту ПЕРЕЗАПИСЫВАЕТ лежащую в очереди.
#: В модель должно приехать последнее решение админа, а не цепочка промежуточных.
COALESCING = frozenset({
    CommandKind.SET_TASK_STATUS,
    CommandKind.SET_PRICE_STATUS,
    CommandKind.EDIT_TASK_DESCRIPTION,
})


@dataclass
class Command:
    """Одна команда визуала.

    `price_id` заполняется и для задачных команд: по нему работает «кто первый» и захват
    прайса, а искать прайс по задаче в момент разбора очереди — лишний круг к базе.
    """
    kind: CommandKind
    source: str = ""                    # «telegram», «1c» — какой провайдер принёс
    actor: str = ""                     # кто из админов; захват принадлежит ему
    price_id: int | None = None
    task_id: int | None = None
    payload: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now)
    id: int | None = None

    @property
    def exclusive(self) -> bool:
        return self.kind in EXCLUSIVE

    @property
    def coalescing(self) -> bool:
        return self.kind in COALESCING

    def coalesce_key(self) -> tuple | None:
        """Ключ схлопывания: вид плюс объект, к которому команда относится.

        None — команда не схлопывается и всегда добавляется отдельной строкой.
        """
        if not self.coalescing:
            return None
        target = self.task_id if self.task_id is not None else self.price_id
        return (self.kind.value, target)

    def label(self) -> str:
        target = (f"задача {self.task_id}" if self.task_id is not None
                  else f"прайс {self.price_id}" if self.price_id is not None else "")
        return f"{self.kind.value} ({target})".strip()


@dataclass(frozen=True)
class Rejected:
    """Команда, которую не взяли в работу, и почему — визуалу надо что-то ответить."""
    command: Command
    reason: str


def order_batch(commands: list[Command]) -> list[Command]:
    """Упорядочить пачку по времени создания на стороне визуала.

    Ничья разрешается порядком в очереди (`id`): две команды с одинаковой отметкой времени
    должны разбираться одинаково при каждом прогоне, иначе поведение зависело бы от того,
    как база вернула строки.
    """
    return sorted(commands, key=lambda c: (c.created_at, c.id or 0))


def plan_batch(commands: list[Command],
               busy_prices: set[int] | None = None) -> tuple[list[Command], list[Rejected]]:
    """Разобрать пачку: что выполняем, что отклоняем.

    Правило «кто первый, тот и работает» (§7): если за цикл пришло несколько ЗАНИМАЮЩИХ
    команд по одному прайсу, выполняется самая ранняя, остальным отвечаем «прайс занят».
    Это тот же жёсткий запрет, что и захват прайса (§4.1), просто на входе.

    `busy_prices` — прайсы, уже захваченные кем-то к началу цикла. Команды по ним
    отклоняются сразу, не дожидаясь своей очереди.

    Команды-присваивания (статус, описание) под правило НЕ попадают: они мгновенны, и
    отвечать «занято» на вторую подряд смену статуса значило бы ломать обычную работу.
    """
    busy = set(busy_prices or ())
    taken: set[int] = set()
    run: list[Command] = []
    rejected: list[Rejected] = []

    for command in order_batch(commands):
        if not command.exclusive:
            run.append(command)
            continue

        price_id = command.price_id
        if price_id is None:
            run.append(command)
            continue

        if price_id in busy:
            rejected.append(Rejected(command, "прайс занят другим администратором"))
        elif price_id in taken:
            rejected.append(Rejected(command, "прайс занят: по нему уже идёт другая команда"))
        else:
            taken.add(price_id)
            run.append(command)

    return run, rejected
