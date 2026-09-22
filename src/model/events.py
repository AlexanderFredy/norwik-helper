"""Уведомления визуалов (§8 specs/agent-workflow-model.md).

Модель объявляет протокол слушателя; провайдеры на него подписываются. Модель при этом не
знает о существовании ни Telegram, ни 1С — в этом весь смысл разделения.

**Текст события формирует КОД, без участия LLM.** Это не экономия, а надёжность:
переписывание готового текста моделью уже стоило ~2 000 выходных токенов на шаг и теряло
строки (§9.6.3 content-manager.md).

**Гарантии доставки нет.** Недоставленные события не копятся: визуал при подключении
перечитывает состояние (§8). Поэтому сбой слушателя не имеет права ронять работу модели.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class EventKind:
    PRICE_ADDED = "прайс добавлен"
    PRICE_REJECTED = "прайс отклонён"
    PRICE_STATUS = "статус прайса изменён"
    PRICE_REMOVED = "прайс уничтожен"
    TASKS_REBUILT = "задачи пересобраны"
    TASK_ADDED = "задача добавлена"
    TASK_STATUS = "статус задачи изменён"
    TASK_RUNNING = "задача взята в работу"
    TASK_REMOVED = "задача удалена"
    LOCK_TAKEN = "прайс захвачен"
    LOCK_RELEASED = "захват снят"
    COMMAND_REJECTED = "команда отклонена"
    # Справочник поставщиков живёт рядом с моделью, но в ЗЕРКАЛО 1С его имена попадают:
    # колонка «Поставщик» в таблице прайсов — это имя из справочника. Без события
    # переименование не доезжало до формы вовсе: снимок шлётся, только когда состояние
    # менялось, а модель о переименовании ничего не знает.
    SUPPLIER_RENAMED = "поставщик переименован"


@dataclass(frozen=True)
class Event:
    kind: str
    text: str                        # готовый текст для админа
    price_id: int | None = None
    task_id: int | None = None
    actor: str = ""                  # кому адресовано; пусто — всем
    data: dict = field(default_factory=dict)


class Listener:
    """Протокол слушателя. Провайдеры наследуют и реализуют `notify`."""

    async def notify(self, event: Event) -> None:  # pragma: no cover - интерфейс
        raise NotImplementedError


class Broadcaster:
    """Рассылка события всем подписчикам.

    Сбой одного слушателя не должен мешать остальным и уж тем более ронять модель: визуал
    и так перечитывает состояние при подключении, а потеря уведомления — не потеря данных.
    """

    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def publish(self, event: Event) -> None:
        for listener in self._listeners:
            try:
                await listener.notify(event)
            except Exception:                       # noqa: BLE001
                logger.warning("Слушатель %s не принял событие %s",
                               type(listener).__name__, event.kind, exc_info=True)
