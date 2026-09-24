"""Запуск поллинга, переживающий недоступность Telegram (бой 24.09.2026).

**У aiogram есть свой повтор, но он начинается ПОЗЖЕ.** Однажды поднявшись, поллинг
переживает обрывы сам: «Failed to fetch updates… Sleep for 1 second… Connection
established». А вот до этого `start_polling` зовёт `bot.me()` — и если в эту секунду не
отвечает DNS, исключение уходит наружу, `asyncio.gather` роняет процесс, и бот лежит,
пока его не поднимут руками. Ровно это и случилось: `getaddrinfo failed` на старте, а
через три минуты тот же бот запустился без единой правки.

**Цикл модели при этом НЕ ДОЛЖЕН умирать вместе с Telegram.** У агента два визуала, и
форма 1С к Telegram не имеет отношения: пока канал до Telegram чинится, команды из формы
обязаны исполняться. Раньше их общий `gather` означал обратное — любая сетевая беда
одного визуала уносила второй.

Сдаёмся мы только на том, что повтором не лечится: неверный токен, отзыв доступа — это
`TelegramUnauthorizedError`, и ждать тут нечего.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramNetworkError

logger = logging.getLogger(__name__)

#: С чего начинаем ждать и докуда растём. Пять секунд — потому что DNS моргает на
#: секунды; минута сверху — чтобы лежащий час канал не давал сотню попыток в минуту.
FIRST_WAIT = 5.0
MAX_WAIT = 60.0


async def poll_forever(dp, bot, sleep=asyncio.sleep) -> None:
    """Крутить поллинг, переподнимая его после сетевых сбоев.

    `sleep` подменяется в тестах: иначе проверка ожидания шла бы через настоящую паузу.
    """
    wait = FIRST_WAIT
    while True:
        try:
            await dp.start_polling(bot)
            return                                      # штатная остановка
        except TelegramNetworkError as exc:
            logger.warning("Telegram недоступен (%s) — повтор через %.0f с. "
                           "Цикл модели и форма 1С продолжают работать", exc, wait)
            await sleep(wait)
            wait = min(wait * 2, MAX_WAIT)
