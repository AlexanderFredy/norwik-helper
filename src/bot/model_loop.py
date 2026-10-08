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

from src.model.commands import CommandKind, Rejected, group_by_actor, plan_batch

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
        """Пауза до следующего оборота.

        ОТКРЫТЫЙ ВИЗУАЛ ДЕРЖИТ БЫСТРЫЙ ТЕМП, даже когда команд давно не было. Простой
        считается пятью минутами без команд — и первое нажатие после паузы ждало до
        тридцати секунд. Это и оказалось главным источником «нажал, и ничего не
        происходит» (разбор 30.09.2026): человек открывает форму, смотрит на неё, потом
        жмёт — то есть ровно в тот момент, когда агент уже уснул.

        Признак приходит из 1С в ответе `agent-commands` и живёт минуту: закрытая форма
        перестаёт его продлевать, и темп падает сам, без события «форма закрылась».
        """
        if any(getattr(p, "visual_active", False) for p in self._providers):
            return ACTIVE_PERIOD
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
        await self._collect()

        batch = await self._queue.take()
        if not batch:
            return 0

        self._last_activity = asyncio.get_event_loop().time()

        # ПРЕРЫВАНИЕ РАЗБИРАЕТСЯ ДО ВСЕГО ОСТАЛЬНОГО и мимо `plan_batch`: это не работа по
        # прайсу, а отмена работы. Пришло вместе с другими командами по тому же прайсу —
        # они не выполняются: «прервать» значит и «не начинать».
        halted: set = set()
        applied = 0
        for command in [c for c in batch if c.kind == CommandKind.INTERRUPT]:
            await self._interrupt(command, halted)
            applied += 1
        batch = [c for c in batch if c.kind != CommandKind.INTERRUPT]

        # Занятость считается ОТДЕЛЬНО ДЛЯ КАЖДОГО инициатора: свой захват админа не
        # блокирует, чужой блокирует (§5.1). Общего набора «занятых» не существует.
        #
        # ГРУППЫ ОБХОДИМ ПО ВРЕМЕНИ НАЖАТИЯ (`group_by_actor`), а не по тому, как строки
        # легли в очередь: первая группа берёт захват, вторая получает «прайс занят
        # другим администратором». По порядку очереди выигрывал бы тот, чьего провайдера
        # опросили раньше, — то самое, что правило «кто первый» (§7) и запрещает.
        for actor, commands in group_by_actor(batch):
            run, rejected = plan_batch(commands, self._service.busy_for(actor))
            for refusal in rejected:
                await self._service.reject(refusal)
                await self._queue.done(refusal.command.id)
            for command in run:
                if command.price_id is not None and command.price_id in halted:
                    await self._service.reject(Rejected(command, "прервано админом"))
                    await self._queue.done(command.id)
                    continue
                await self._apply_watched(command, halted)
                await self._queue.done(command.id)
                applied += 1
                await self._report(halted)

        return applied

    async def _report(self, halted: set) -> None:
        """Отчитаться визуалам СРАЗУ после команды, не дожидаясь конца пачки.

        Админ отмечает пять задач и жмёт «Выполнить» один раз; исполняются они по очереди.
        Снимок и исход команд уезжали в 1С только на следующем обороте — то есть после
        ПОСЛЕДНЕЙ задачи, и статусы всех пяти менялись разом (вопрос админа 08.10.2026).
        Опрос провайдера это и есть доставка: он отправляет снимок, закрывает команды,
        которых нет в очереди, — а заодно забирает прерывание, присланное между задачами.
        """
        await self._collect()
        for stop in await self._queue.take(kinds={CommandKind.INTERRUPT}):
            await self._interrupt(stop, halted)

    async def _collect(self) -> None:
        for provider in self._providers:
            try:
                await provider.collect(self._queue)
            except Exception:                           # noqa: BLE001
                logger.warning("Провайдер %s не отдал команды",
                               type(provider).__name__, exc_info=True)

    async def _interrupt(self, command, halted: set) -> None:
        """Прервать работу по прайсу. Исход у команды один — «выполнена».

        Нечего прерывать — тоже «выполнена», без отказа: желаемое состояние «ничего не
        идёт» уже достигнуто. А отказ сопоставляется с формой по (инициатор, прайс,
        задача) — и совпал бы с командой, которую прервали.
        """
        halted.add(command.price_id)
        self._service.interrupt(command.price_id)
        await self._queue.done(command.id)

    async def _apply_watched(self, command, halted: set) -> None:
        """Применить команду, НЕ ПЕРЕСТАВАЯ СЛУШАТЬ визуалы.

        Прежде команда выполнялась прямо в обороте, и пока шла сборка задач или задача —
        минуты, на плохом VPN десятки минут, — агент не опрашивал очередь вовсе. Нажать
        «Прервать» было можно, услышать — некому (08.10.2026). Теперь работа идёт
        отдельной asyncio-задачей, а цикл раз в `ACTIVE_PERIOD` забирает ТОЛЬКО
        прерывания: прочие команды ждут конца прогона, как и раньше, — порядок «кто
        первый» и захваты от этого не меняются. Попутно опрос держит в 1С метку «агент
        жив», и форма перестаёт подозревать агента, занятого длинной работой.
        """
        job = asyncio.ensure_future(self._service.apply(command))
        try:
            while True:
                done, _ = await asyncio.wait({job}, timeout=ACTIVE_PERIOD)
                if done:
                    break
                await self._collect()
                for stop in await self._queue.take(kinds={CommandKind.INTERRUPT}):
                    await self._interrupt(stop, halted)
        except asyncio.CancelledError:
            # Останавливают сам цикл — работу тоже останавливаем, а не бросаем сиротой.
            job.cancel()
            raise
        if job.cancelled():
            logger.warning("Команда %s отменена, не дойдя до конца", command.label())
        else:
            job.result()        # выпустить то, что `apply` не поймал, как и раньше
