"""Перечисления модели (§3 specs/agent-workflow-model.md).

Строковые значения намеренно: они уходят в БД, в команды визуалов и в промпт LLM, и
читаемое «изменение цен» там полезнее, чем «4».
"""
from __future__ import annotations

from enum import Enum


class TaskKind(str, Enum):
    """Виды задач в ПОРЯДКЕ СОРТИРОВКИ.

    Порядок — подсказка админу, а не запрет: выполнять можно в любом. Но сам порядок не
    произвольный: «перенос в снятые» стоит ПЕРЕД «добавлением новых», потому что перед
    созданием позиции обязательна проверка, нет ли её среди снятых, — иначе заводится дубль
    (§19.11 content-manager.md).
    """
    NORMALIZE_NAMES = "нормализация наименований"
    CHANGE_PROPERTIES = "изменение свойств"
    MOVE_DISCONTINUED = "перенос в снятые"
    ADD_NEW = "добавление новых"
    CHANGE_PRICES = "изменение цен"

    @property
    def order(self) -> int:
        return _KIND_ORDER[self]


_KIND_ORDER = {kind: i for i, kind in enumerate(TaskKind)}


class TaskSubject(str, Enum):
    COLLECTION = "коллекция"
    ITEM = "товар"


class TaskStatus(str, Enum):
    """Три статуса, четвёртого нет.

    `PARTIAL` — информационный и ШТАТНЫЙ исход: часть задуманного не удалась, и в результате
    обязательны причина и что именно сделано. Если не получилось ничего — задача остаётся
    `TODO`, а не становится `PARTIAL`.
    """
    TODO = "к обработке"
    DONE = "выполнена"
    PARTIAL = "частично обработана"

    @property
    def closed(self) -> bool:
        """Считается ли задача закрытой для готовности прайса (§3.2)."""
        return self in (TaskStatus.DONE, TaskStatus.PARTIAL)


class PriceStatus(str, Enum):
    """Статус прайса ставит АДМИН; модель его не выставляет сама.

    Модель считает только готовность (`Price.ready`). Если бы статус выводился
    автоматически, одна задача с исходом `PARTIAL` запирала бы прайс навсегда.
    """
    TODO = "к обработке"
    PARTIAL = "частично обработан"
    DONE = "выполнен"
