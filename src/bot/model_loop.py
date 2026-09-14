"""Цикл агента: опрос визуалов и применение команд (§9 specs/agent-workflow-model.md).

Агент забирает команды сам. Слушателя на VPS не поднимаем: это открытая наружу точка входа,
и он всё равно не избавляет от очереди на стороне 1С — иначе команда теряется при
перезапуске.

**Период адаптивный** (§9.1): 5 секунд, пока была активность в последние 5 минут, иначе 30.
Круглосуточные пять секунд оплачивали бы занятость 1С ради часов, когда никто не работает.

**Telegram цикла не ждёт.** `aiogram` доставляет событие сразу, и ТГ-провайдер, положив
команду в очередь, будит цикл через `wake()`. Иначе в простое админ ждал бы полминуты на
команду, набранную в чате. Адаптивный период управляет только частотой похода в 1С — тем
единственным опросом, который мы инициируем сами и который что-то стоит.
"""
from __future__ import annotations

import asyncio
import logging

from src.model.commands import plan_batch

logger = logging.getLogger(__name__)

ACTIVE_PERIOD = 5.0        # секунд, пока идёт работа
IDLE_PERIOD = 30.0         # секунд в простое
ACTIVE_WINDOW = 300.0      # столько считаем «работа ещё идёт» после последней команды


class AgentLoop:
    """Один оборот: разбудили или дождались периода → забрали команды → применили."""

    def __init__(self, queue, service, providers=None) -> None:
        self._queue = queue
        self._service = service
        self._providers = list(providers or [])
        self._wake = asyncio.Event()
        self._last_activity = 0.0
        self._stopped = False

    def wake(self) -> None:
        """Разбудить цикл немедленно. Зовёт ТГ-провайдер, положив команду."""
        self._wake.set()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    @property
    def period(self) -> float:
        idle = asyncio.get_event_loop().time() - self._last_activity
        return ACTIVE_PERIOD if idle < ACTIVE_WINDOW else IDLE_PERIOD

    async def run(self) -> None:
        logger.info("Цикл модели запущен")
        while not self._stopped:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.period)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._stopped:
                break
            try:
                await self.tick()
            except Exception:                           # noqa: BLE001
                logger.exception("Оборот цикла модели сорвался")

    async def tick(self) -> int:
        """Один оборот. Возвращает число применённых команд — удобно для тестов."""
        # Истёкшие захваты снимаем ПЕРВЫМ делом: иначе прайс, заброшенный админом, до конца
        # оборота считался бы занятым и отклонил бы чужие команды (§5.1).
        await self._service.expire_locks()

        for provider in self._providers:
            try:
                await provider.collect(self._queue)
            except Exception:                           # noqa: BLE001
                logger.warning("Провайдер %s не отдал команды",
                               type(provider).__name__, exc_info=True)

        batch = await self._queue.take()
        if not batch:
            return 0

        self._last_activity = asyncio.get_event_loop().time()

        # Занятость считается ОТДЕЛЬНО ДЛЯ КАЖДОГО инициатора: свой захват админа не
        # блокирует, чужой блокирует (§5.1). Общего набора «занятых» не существует.
        applied = 0
        by_actor: dict[str, list] = {}
        for command in batch:
            by_actor.setdefault(command.actor, []).append(command)

        for actor, commands in by_actor.items():
            run, rejected = plan_batch(commands, self._service.busy_for(actor))
            for refusal in rejected:
                await self._service.reject(refusal)
                await self._queue.done(refusal.command.id)
            for command in run:
                await self._service.apply(command)
                await self._queue.done(command.id)
                applied += 1

        return applied
