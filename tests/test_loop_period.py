"""Темп оборотов цикла: когда агент спешит, а когда спит (разбор 30.09.2026).

Задержка нажатия складывается из двух ног: агент забирает команду (5 с при работе, 30 с в
простое) и форма замечает результат своим таймером (5 с). Первая нога и была главной болью:
простоем считаются пять минут без команд, а человек открывает форму, смотрит на неё и
жмёт — то есть ровно тогда, когда агент уже уснул.
"""
import unittest

from src.bot.model_loop import ACTIVE_PERIOD, ACTIVE_WINDOW, IDLE_PERIOD, AgentLoop


class Visual:
    """Провайдер, который знает, открыт ли его визуал."""

    def __init__(self, active=False):
        self.visual_active = active


class Deaf:
    """Провайдер без такого признака — например, телеграмный."""


class PeriodTest(unittest.IsolatedAsyncioTestCase):
    """Тесты асинхронные, потому что `period` берёт время у цикла событий."""


    def loop(self, *providers):
        return AgentLoop(queue=None, service=None, providers=list(providers))

    def idle(self, loop):
        """Отодвинуть последнюю активность за окно простоя."""
        loop._last_activity = -(ACTIVE_WINDOW + 1)
        return loop

    async def test_idle_without_visuals(self):
        self.assertEqual(self.idle(self.loop()).period, IDLE_PERIOD)

    async def test_open_form_keeps_the_fast_pace(self):
        loop = self.idle(self.loop(Visual(active=True)))
        self.assertEqual(loop.period, ACTIVE_PERIOD)

    async def test_closed_form_does_not_keep_it(self):
        loop = self.idle(self.loop(Visual(active=False)))
        self.assertEqual(loop.period, IDLE_PERIOD)

    async def test_one_open_visual_is_enough(self):
        loop = self.idle(self.loop(Visual(active=False), Visual(active=True)))
        self.assertEqual(loop.period, ACTIVE_PERIOD)

    async def test_provider_without_the_flag_does_not_break_anything(self):
        """Телеграмный провайдер про визуалы 1С ничего не знает, и спрашивать его нечего."""
        loop = self.idle(self.loop(Deaf()))
        self.assertEqual(loop.period, IDLE_PERIOD)

    async def test_recent_command_still_wins_on_its_own(self):
        """Прежнее правило никуда не делось: работа идёт — темп быстрый и без формы."""
        import asyncio

        loop = self.loop()
        # Часы цикла событий монотонные, и ноль в них — не «сейчас», а момент запуска
        # машины: отметку надо ставить текущим значением, иначе она читается как давняя.
        loop._last_activity = asyncio.get_event_loop().time()
        self.assertEqual(loop.period, ACTIVE_PERIOD)


if __name__ == "__main__":
    unittest.main()
