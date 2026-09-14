"""Неразобранная команда не должна уходить менеджерскому агенту.

Общий обработчик ловит ЛЮБОЙ текст, поэтому команда, которую никто не разобрал, попадала бы
к нему — это вызов модели за деньги и ответ про поиск товара на «/queue». Случай не
умозрительный: команды прежнего прайсового потока отключены вместе с ним, а админ их помнит
и набирает.
"""
import unittest

from src.bot import handlers


class FakeMessage:
    def __init__(self, text: str):
        self.text = text
        self.from_user = type("U", (), {"id": 42})()
        self.sent: list[str] = []

    async def answer(self, text, **kw):
        self.sent.append(text)
        return self

    @property
    def last(self) -> str:
        return self.sent[-1] if self.sent else ""


class UnknownCommandTest(unittest.IsolatedAsyncioTestCase):

    async def test_disabled_command_is_named_not_denied(self):
        """«Не знаю такой команды» про /queue, которая вчера работала, сбивает с толку."""
        msg = FakeMessage("/queue")
        await handlers.handle_unknown_command(msg)
        self.assertIn("отключён", msg.last)
        self.assertIn("/prices", msg.last)

    async def test_truly_unknown_command(self):
        msg = FakeMessage("/чегоизволите")
        await handlers.handle_unknown_command(msg)
        self.assertIn("Не знаю команду", msg.last)

    async def test_arguments_do_not_break_recognition(self):
        msg = FakeMessage("/deferred_resume 3")
        await handlers.handle_unknown_command(msg)
        self.assertIn("отключён", msg.last)

    async def test_bot_suffix_is_stripped(self):
        """В группах Telegram команда приходит как /queue@norwik_bot."""
        msg = FakeMessage("/queue@norwik_bot")
        await handlers.handle_unknown_command(msg)
        self.assertIn("отключён", msg.last)

    async def test_handler_stands_before_the_catch_all(self):
        """Порядок в роутере важнее самого обработчика: встань он после общего —
        команда всё равно ушла бы агенту."""
        names = [h.callback.__name__ for h in handlers.router.message.handlers]
        self.assertLess(names.index("handle_unknown_command"), names.index("handle_query"))


if __name__ == "__main__":
    unittest.main()
