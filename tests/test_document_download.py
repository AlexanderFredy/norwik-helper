"""Приём файла прайса при плохой связи с Telegram (бой 27.10.2026).

Файл не скачался за 64 с, бот промолчал, и админ час ждал разбора, которого не было.
Теперь скачивание повторяется, а неудача называется вслух.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.bot import model_handlers as mh


class FakeBot:
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    async def get_file(self, file_id):
        return SimpleNamespace(file_path="documents/x.xlsx")

    async def download_file(self, path, destination):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError()
        destination.write(b"PK-price")


class FakeMessage:
    def __init__(self, bot, fail_answers=False):
        self.bot = bot
        self.document = SimpleNamespace(file_name="Прайс.xlsx", file_size=100, file_id="F")
        self.caption = "Поставщик"
        self.from_user = SimpleNamespace(id=1)
        self.sent = []
        self.fail_answers = fail_answers

    async def answer(self, text):
        if self.fail_answers:
            raise TimeoutError()
        self.sent.append(text)
        return SimpleNamespace(delete=self._noop)

    async def _noop(self):
        pass


class FakeModel:
    def __init__(self):
        self.got = []

    async def submit(self, content, name, **kw):
        self.got.append((content, name))


async def no_sleep(_seconds):
    pass


class DownloadTest(unittest.IsolatedAsyncioTestCase):

    async def run_handler(self, fail_times, fail_answers=False):
        model = FakeModel()
        msg = FakeMessage(FakeBot(fail_times), fail_answers)
        with patch.object(mh.asyncio, "sleep", no_sleep):
            await mh.handle_document(msg, model, is_admin=True)
        return msg, model

    async def test_retry_gets_the_file(self):
        msg, model = await self.run_handler(fail_times=2)
        self.assertEqual(model.got, [(b"PK-price", "Прайс.xlsx")])

    async def test_failure_is_told_not_swallowed(self):
        msg, model = await self.run_handler(fail_times=99)
        self.assertEqual(model.got, [])
        self.assertTrue(any("не получен" in t for t in msg.sent), msg.sent)
        self.assertEqual(msg.bot.calls, len(mh.DOWNLOAD_PAUSES) + 1)

    async def test_failed_answers_do_not_break_intake(self):
        """Сообщение «Принимаю прайс…» не ушло — файл всё равно скачивается и разбирается."""
        msg, model = await self.run_handler(fail_times=0, fail_answers=True)
        self.assertEqual(len(model.got), 1)


if __name__ == "__main__":
    unittest.main()
