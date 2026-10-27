"""Прайс из формы 1С и лента сообщений агента (решение админа 27.10.2026).

27.10.2026 связь сервера с Telegram пропала на полдня: прайс, присланный в бот, не
скачался, а ответы агента до админа не доходили. Теперь и то и другое ходит через форму.
"""
import asyncio
import base64
import tempfile
import unittest
from pathlib import Path

from src.model.commands import Command, CommandKind
from src.model.events import Event, EventKind
from src.onec.model_provider import OnecProvider
from src.storage.command_queue import CommandQueue
from src.storage.onec_outbox import OnecOutbox
from tests.test_onec_provider import FakeService, command


class UploadOnec:
    def __init__(self, commands=(), files=None, messages_error=None):
        self.commands = list(commands)
        self.files = dict(files or {})
        self.states, self.messages = [], []
        self.messages_error = messages_error

    def agent_commands(self):
        out, self.commands = self.commands, []
        return {"server_time": None, "commands": out}

    def agent_commands_state(self, items):
        self.states.append(list(items))
        return {"updated": len(items)}

    def set_model_state(self, prices, sheets=None, marks=None):
        return {"changed": 0}

    def agent_file(self, command_id):
        if command_id not in self.files:
            return {"error": "not_found"}
        name, content = self.files[command_id]
        return {"name": name, "supplier": "Ника",
                "data": base64.b64encode(content).decode()}

    def agent_messages(self, items):
        if self.messages_error:
            return {"error": self.messages_error}
        self.messages.extend(items)
        return {"written": len(items)}


class Base(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.queue = CommandQueue(self.root / "q.db")
        await self.queue.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    def provider(self, onec, outbox=None):
        return OnecProvider(onec, FakeService(), outbox=outbox, inbox_dir=self.root / "in")


class UploadTest(Base):

    def upload(self, external="up1"):
        return command(external=external, kind=CommandKind.SUBMIT_PRICE.value, price_id=0,
                       task_id=0, payload={"file": "ЭГГЕР.xls", "supplier": "Ника"})

    async def test_file_lands_on_disk_and_command_goes_to_the_queue(self):
        onec = UploadOnec([self.upload()], files={"up1": ("ЭГГЕР.xls", b"XLS-bytes")})
        await self.provider(onec).collect(self.queue)

        pending = await self.queue.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].kind, CommandKind.SUBMIT_PRICE)
        path = Path(pending[0].payload["path"])
        self.assertEqual(path.read_bytes(), b"XLS-bytes")
        self.assertEqual(path.suffix, ".xls")
        self.assertEqual(pending[0].payload["filename"], "ЭГГЕР.xls")
        self.assertEqual(pending[0].payload["supplier"], "Ника")
        self.assertEqual([i["state"] for b in onec.states for i in b], ["принята"])

    async def test_missing_file_is_refused_not_left_waiting(self):
        onec = UploadOnec([self.upload()])
        await self.provider(onec).collect(self.queue)
        self.assertEqual(await self.queue.pending(), [])
        states = [(i["state"], i["message"]) for b in onec.states for i in b]
        self.assertEqual(states[0][0], "отклонена")
        self.assertIn("не найден", states[0][1])

    async def test_network_failure_leaves_the_command_for_the_next_poll(self):
        """Сеть моргнула — команда в 1С остаётся «ждёт» и приедет снова."""
        class Broken(UploadOnec):
            def agent_file(self, command_id):
                raise ConnectionError("оборвалось")

        onec = Broken([self.upload()])
        await self.provider(onec).collect(self.queue)
        self.assertEqual(await self.queue.pending(), [])
        self.assertEqual(onec.states, [], "ни «принята», ни «отклонена»")

    async def test_null_answer_is_refused_not_retried_forever(self):
        """1С отвечает `null` (маршрут не тот) — отказ с причиной, а не вечное «ждёт»."""
        class Null(UploadOnec):
            def agent_file(self, command_id):
                return None

        onec = Null([self.upload()])
        await self.provider(onec).collect(self.queue)
        self.assertEqual(await self.queue.pending(), [])
        states = [(i["state"], i["message"]) for b in onec.states for i in b]
        self.assertEqual(states[0][0], "отклонена")
        self.assertIn("agent-file", states[0][1])

    async def test_rejected_intake_closes_the_command_as_refused(self):
        """Отказ приёма (устаревший прайс) — отказ команды, с причиной."""
        onec = UploadOnec([self.upload()], files={"up1": ("ЭГГЕР.xls", b"x")})
        provider = self.provider(onec)
        await provider.collect(self.queue)
        taken = await self.queue.take()
        await provider.notify(Event(EventKind.PRICE_REJECTED, actor="1c:Иванов",
                                    text="Прайс «ЭГГЕР.xls» не принят: есть более свежий."))
        await self.queue.done(taken[0].id)
        await provider.collect(self.queue)
        final = [i for b in onec.states for i in b if i["state"] != "принята"]
        self.assertEqual(final[0]["state"], "отклонена")
        self.assertIn("более свежий", final[0]["message"])


class FileHashTest(Base):
    """Хеш содержимого в снимке — по нему форма узнаёт повторно загружаемый файл."""

    def price(self, path):
        from src.model.price import Price, SupplierPrice
        return Price(supplier_price=SupplierPrice(supplier_id=1, file_id=1,
                                                  file_path=str(path), filename="Прайс.xlsx"),
                     id=568)

    async def test_snapshot_carries_sha256_of_the_content(self):
        import hashlib
        path = self.root / "p.xlsx"
        path.write_bytes(b"XLS-bytes")
        provider = OnecProvider(UploadOnec(), FakeService([self.price(path)]))
        row = (await provider.snapshot())[0]
        self.assertEqual(row["file_hash"], hashlib.sha256(b"XLS-bytes").hexdigest())

    async def test_missing_file_gives_empty_hash_not_an_error(self):
        provider = OnecProvider(UploadOnec(), FakeService([self.price(self.root / "нет.xlsx")]))
        self.assertEqual((await provider.snapshot())[0]["file_hash"], "")


class ServiceUploadTest(unittest.IsolatedAsyncioTestCase):

    async def test_service_submits_and_removes_the_temp_file(self):
        from src.model.service import PriceListService

        got = []

        class Service(PriceListService):
            def __init__(self):
                from src.model.events import Broadcaster
                self.events = Broadcaster()

            async def submit(self, content, filename, **kw):
                got.append((content, filename, kw))
                return ""

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.xls"
            path.write_bytes(b"data")
            await Service().apply(Command(
                kind=CommandKind.SUBMIT_PRICE, source="1c", actor="1c:Иванов",
                payload={"path": str(path), "filename": "ЭГГЕР.xls", "supplier": "Ника"}))
            self.assertFalse(path.exists(), "временная копия удалена")
        self.assertEqual(got[0][:2], (b"data", "ЭГГЕР.xls"))
        self.assertEqual(got[0][2]["supplier_hint"], "Ника")
        self.assertEqual(got[0][2]["actor"], "1c:Иванов")


class MessagesTest(Base):

    async def outbox(self):
        box = OnecOutbox(self.root / "q.db")
        await box.init()
        return box

    async def test_admin_messages_reach_1c(self):
        onec = UploadOnec()
        box = await self.outbox()
        provider = self.provider(onec, box)
        await provider.notify(Event(EventKind.PRICE_ADDED, price_id=12,
                                    text="Прайс «ЭГГЕР.xls» принят (№12)."))
        await provider.collect(self.queue)
        self.assertEqual([(m["text"], m["price_id"]) for m in onec.messages],
                         [("Прайс «ЭГГЕР.xls» принят (№12).", 12)])
        self.assertEqual(await box.pending(), [], "доставленное снято")

    async def test_failed_delivery_keeps_the_message(self):
        """Старый BSL маршрута не знает — сообщения дождутся выкладки, а не пропадут."""
        onec = UploadOnec(messages_error="unknown query-type")
        box = await self.outbox()
        provider = self.provider(onec, box)
        await provider.notify(Event(EventKind.TASKS_REBUILT, text="Задачи пересобраны."))
        await provider.collect(self.queue)
        self.assertEqual(len(await box.pending()), 1)

    async def test_silent_and_manager_events_are_not_posted(self):
        onec = UploadOnec()
        box = await self.outbox()
        provider = self.provider(onec, box)
        await provider.notify(Event(EventKind.SUPPLIER_RENAMED, text=""))
        await provider.notify(Event(EventKind.PRICE_SUMMARY, text="Сводка менеджерам"))
        self.assertEqual(await box.pending(), [])

    async def test_outbox_survives_and_is_bounded(self):
        from src.storage import onec_outbox
        box = await self.outbox()
        for n in range(onec_outbox.MAX_KEPT + 5):
            await box.add(f"сообщение {n}")
        pending = await box.pending(limit=1000)
        self.assertEqual(len(pending), onec_outbox.MAX_KEPT)
        self.assertEqual(pending[0]["text"], "сообщение 5", "старейшие вытеснены")


class TelegramListenerTest(unittest.IsolatedAsyncioTestCase):

    async def test_slow_telegram_does_not_hold_the_model(self):
        """Пока Telegram недоступен, публикация события не ждёт таймаута сети."""
        from src.bot.model_handlers import TelegramListener

        class SlowBot:
            async def send_message(self, chat, text):
                await asyncio.sleep(30)

        listener = TelegramListener(SlowBot(), [1])
        await asyncio.wait_for(listener.notify(Event(EventKind.PRICE_ADDED, text="x")), 1)
        for task in list(listener._pending):
            task.cancel()


if __name__ == "__main__":
    unittest.main()
