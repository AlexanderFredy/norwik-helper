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
from datetime import datetime, timedelta, timezone
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


#: Потолок доверия к метке — СТРАХОВКА, а не основной механизм. Основную работу делает
#: измеренное смещение часов источника; потолок ловит случай, когда измерить не вышло или
#: измерение само оказалось мусором. Час, а не пять минут: команда могла честно пролежать в
#: очереди 1С, пока агент был выключен, и такую метку портить нельзя — она верна.
MAX_SKEW_SECONDS = 3600


def sort_time(reported: str | None, offset_seconds: float = 0.0,
              agent_now: datetime | None = None, trust: bool = True) -> tuple[str, str]:
    """Время, по которому команда участвует в «кто первый». Возвращает (время, причина).

    **Зачем вообще поправка.** Порядок решает время создания НА СТОРОНЕ ВИЗУАЛА (§7), но
    часы визуала могут быть сбиты — тогда он либо всегда выигрывает, либо всегда проигрывает,
    и вся затея с честным порядком рушится.

    Расхождение возможно ровно у ОДНОГО визуала — 1С: она отдельный сервер. Команды
    Telegram создаёт сам процесс бота, и их метка — это и есть часы агента, смещение нулевое
    по построению.

    Поэтому `offset_seconds` — измеренное смещение часов источника относительно наших
    (`время_визуала − наше_время` в момент опроса). Вычитая его, приводим метку к нашим
    часам.

    **Потолок доверия — страховка.** Основную работу делает поправка; потолок ловит случай,
    когда смещение измерить не вышло (`trust=False`) или оно само оказалось мусором. Порог
    намеренно велик: команда могла честно пролежать в очереди 1С, пока агент был выключен,
    и такая метка ВЕРНА — портить её нельзя.

    Причина возвращается наружу, чтобы подмена времени была видна в логе, а не молча меняла
    порядок.
    """
    agent_now = agent_now or datetime.now(timezone.utc)
    if not trust:
        return agent_now.isoformat(), "часы источника неизвестны"
    if not reported:
        return agent_now.isoformat(), "метки нет"

    try:
        stamp = datetime.fromisoformat(reported)
    except ValueError:
        return agent_now.isoformat(), "метка не разобрана"

    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)

    corrected = stamp - timedelta(seconds=offset_seconds)
    drift = abs((corrected - agent_now).total_seconds())
    if drift > MAX_SKEW_SECONDS:
        return agent_now.isoformat(), f"часы источника разошлись на {int(drift)} с"
    return corrected.isoformat(), ""


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
    created_at: str = field(default_factory=now)     # как сообщил визуал — для диагностики
    sort_at: str = ""                                # приведённое к нашим часам (`sort_time`)
    id: int | None = None

    def __post_init__(self) -> None:
        # Без поправки порядок считается по сырой метке: для Telegram это одно и то же,
        # потому что её ставит тот же процесс.
        if not self.sort_at:
            self.sort_at = self.created_at

    def coalesce_key(self) -> tuple | None:
        """Ключ замещения: **вид плюс объект**, к которому команда относится.

        Новая команда замещает лежащую в очереди того же вида по тому же объекту: в модель
        должно приехать последнее решение админа, а не цепочка промежуточных. Это касается
        ВСЕХ видов, а не только присваиваний: два «выполни задачу 5» подряд — одно
        намерение, и запускать её дважды не нужно (§4.2).

        **Вид входит в ключ обязательно.** Иначе «изменить описание» и следом «выполнить»
        по одной задаче схлопнулись бы в одно, а это ровно тот рабочий порядок, ради
        которого правка описания и существует: админ правит задание и тут же отправляет
        его на исполнение (§3.3). Потеряв первую команду, мы отправили бы старый текст.

        None — замещать не по чему: у команды нет объекта (приём нового файла), и две
        такие команды это два разных файла.
        """
        target = self.task_id if self.task_id is not None else self.price_id
        if target is None:
            return None
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

    Сортируем по `sort_at` — метке, приведённой к нашим часам (`sort_time`), а не по сырой:
    сбитые часы визуала иначе давали бы ему вечное преимущество.

    Ничья разрешается порядком в очереди (`id`): две команды с одинаковой отметкой времени
    должны разбираться одинаково при каждом прогоне, иначе поведение зависело бы от того,
    как база вернула строки.
    """
    return sorted(commands, key=lambda c: (c.sort_at or c.created_at, c.id or 0))


def plan_batch(commands: list[Command],
               busy_prices: set[int] | None = None) -> tuple[list[Command], list[Rejected]]:
    """Разобрать пачку: что выполняем, что отклоняем.

    **Правило «кто первый» действует на ВСЕ команды** (§7): если за цикл пришло несколько
    команд по одному прайсу, выполняется самая ранняя, остальным отвечаем «прайс занят».
    Тот же жёсткий запрет, что и захват прайса (§5.1), просто на входе.

    Две команды одного вида по ОДНОМУ объекту до этого правила не доходят — они замещают
    друг друга ещё в очереди (`coalesce_key`), и в пачке остаётся одна.

    `busy_prices` — прайсы, закрытые для инициатора к началу цикла. Считать их должен
    вызывающий: захват свой же админ не блокирует, поэтому набор зависит от того, ЧЕЙ это
    цикл (`locks.busy_for`).

    Команда без прайса (приём нового файла) под правило не попадает: она ещё не относится
    ни к какому прайсу.
    """
    busy = set(busy_prices or ())
    taken: set[int] = set()
    run: list[Command] = []
    rejected: list[Rejected] = []

    for command in order_batch(commands):
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
