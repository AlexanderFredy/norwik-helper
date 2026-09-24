"""Поллинг переживает недоступность Telegram (бой 24.09.2026).

Собственный повтор aiogram начинается ПОЗЖЕ — однажды поднявшись, поллинг чинит обрывы
сам. А `start_polling` сперва зовёт `bot.me()`, и `getaddrinfo failed` в эту секунду
уносил весь процесс: бот лежал, пока его не подняли руками, хотя через три минуты тот же
запуск прошёл без правок.
"""
import unittest

from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

from src.bot.polling import poll_forever


class FakeDispatcher:
    """Падает заданное число раз, потом работает."""

    def __init__(self, failures, error=None):
        self.left = failures
        self.error = error or TelegramNetworkError(method=None, message="DNS упал")
        self.starts = 0

    async def start_polling(self, bot):
        self.starts += 1
        if self.left:
            self.left -= 1
            raise self.error


class PollingTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.waited = []

        async def sleep(seconds):
            self.waited.append(seconds)

        self.sleep = sleep

    async def test_network_failure_at_startup_is_survived(self):
        dp = FakeDispatcher(failures=2)
        await poll_forever(dp, bot=object(), sleep=self.sleep)

        self.assertEqual(dp.starts, 3, "две неудачи и успешный подъём")
        self.assertEqual(len(self.waited), 2)

    async def test_waiting_grows_but_stays_bounded(self):
        """Лежащий час канал не должен давать сотню попыток в минуту."""
        from src.bot.polling import FIRST_WAIT, MAX_WAIT

        dp = FakeDispatcher(failures=12)
        await poll_forever(dp, bot=object(), sleep=self.sleep)

        self.assertEqual(self.waited[0], FIRST_WAIT)
        self.assertGreater(self.waited[-1], self.waited[0])
        self.assertLessEqual(max(self.waited), MAX_WAIT)

    async def test_bad_token_is_not_retried(self):
        """Повтором не лечится: ждать тут нечего, и молчать об этом нельзя."""
        dp = FakeDispatcher(failures=1,
                            error=TelegramUnauthorizedError(method=None, message="токен"))
        with self.assertRaises(TelegramUnauthorizedError):
            await poll_forever(dp, bot=object(), sleep=self.sleep)
        self.assertEqual(self.waited, [])

    async def test_clean_stop_returns(self):
        dp = FakeDispatcher(failures=0)
        await poll_forever(dp, bot=object(), sleep=self.sleep)
        self.assertEqual(dp.starts, 1)


if __name__ == "__main__":
    unittest.main()
