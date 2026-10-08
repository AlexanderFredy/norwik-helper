"""Прерывание работы по прайсу — сборки задач и выполняемой задачи (решение админа 08.10.2026).

На плохом VPN сборка «подвисала», и остановить её было нечем: цикл выполнял команду прямо в
обороте и очередь в это время не слушал вовсе. Проверяется вся обвязка на настоящих
объектах: очередь, служба, цикл; модель и 1С заменены ожиданием, которое не кончается
никогда, — ровно так выглядит зависший запрос.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.bot import model_loop
from src.bot.model_loop import AgentLoop
from src.model.commands import Command, CommandKind
from src.model.enums import TaskStatus
from src.model.events import Broadcaster, EventKind
from src.model.executor import WriteRefused
from src.model.service import PriceListService
from src.storage import price_files
from src.storage.command_queue import CommandQueue
from src.storage.model_store import ModelStore
from src.storage.suppliers import SupplierStore
from tests.test_model_flow import XLSX, Collector

WAIT = 5.0          # предел ожидания в тестах; при верном коде всё кончается за доли секунды


class InterruptTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "t.db"
        self.model_store = ModelStore(self.db)
        await self.model_store.init()
        self.suppliers = SupplierStore(self.db)
        await self.suppliers.init()
        self.queue = CommandQueue(self.db)
        await self.queue.init()

        self.seen = Collector()
        bus = Broadcaster()
        bus.subscribe(self.seen)

        self.hang = False
        self.started = asyncio.Event()
        self.builds = 0
        self.guard = None

        async def build(content, filename, price):
            from src.model.intake import read_signature, stub_tasks
            self.builds += 1
            if self.hang:
                self.started.set()
                await asyncio.Event().wait()
            _, sheets = read_signature(content, filename)
            return stub_tasks(sheets, "Монарх"), ""

        async def run(price, task, content, guard):
            self.guard = guard
            self.started.set()
            await asyncio.Event().wait()

        self.model = PriceListService(
            self.model_store, self.suppliers,
            save_file=lambda content, name: price_files.save(self.db, name, content),
            broadcaster=bus, build_tasks=build, run_task=run)
        await self.model.load()
        self.loop = AgentLoop(self.queue, self.model)

        # Сторож цикла опрашивает очередь раз в ACTIVE_PERIOD — пять секунд. В тесте ждать
        # их незачем: проверяется, что опрос ЕСТЬ, а не его темп.
        patcher = mock.patch.object(model_loop, "ACTIVE_PERIOD", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)

        await self.model.submit(XLSX, "Прайс Монарх 15.09.2026.xlsx", supplier_hint="Монарх",
                                actor="admin-1")
        self.price = self.model.prices[0]

    async def asyncTearDown(self):
        self._dir.cleanup()

    def command(self, kind, **kw):
        return Command(kind=kind, source="1c", actor="admin-1", price_id=self.price.id, **kw)

    async def interrupt_midway(self, kind, **kw):
        """Запустить команду, дождаться, пока она «зависнет», и прислать прерывание."""
        await self.queue.put(self.command(kind, **kw))
        tick = asyncio.ensure_future(self.loop.tick())
        await asyncio.wait_for(self.started.wait(), WAIT)
        await self.queue.put(self.command(CommandKind.INTERRUPT))
        await asyncio.wait_for(tick, WAIT)

    async def left_in_queue(self):
        return await self.queue.pending() + await self.queue.taken()

    # ------------------------------------------------------------------ сборка

    async def test_a_hung_rebuild_stops_and_keeps_the_old_tasks(self):
        before = [t.id for t in self.price.tasks]
        self.hang = True

        await self.interrupt_midway(CommandKind.REBUILD_TASKS)

        self.assertEqual([t.id for t in self.price.tasks], before,
                         "прерванная сборка не имеет права заменить список")
        self.assertIn("прервана", self.seen.texts())
        self.assertEqual(await self.left_in_queue(), [],
                         "и сборка, и прерывание закрыты — иначе в форме горит колесико")

    async def test_interrupting_a_rebuild_is_not_reported_as_a_refusal(self):
        """Отказ по прайсу совпал бы адресом с командой прерывания, и в форме 1С та
        закрылась бы «отклонённой» — на нажатие, которое сработало."""
        self.hang = True
        self.seen.events.clear()
        await self.interrupt_midway(CommandKind.REBUILD_TASKS)
        self.assertNotIn(EventKind.COMMAND_REJECTED, self.seen.kinds())

    async def test_a_waiting_rebuild_is_replaced_and_never_starts(self):
        """Не взятая сборка того же прайса замещается прерыванием в очереди — «прервать»
        значит и «не начинать»."""
        self.hang = True
        builds = self.builds
        await self.queue.put(self.command(CommandKind.REBUILD_TASKS))
        await self.queue.put(self.command(CommandKind.INTERRUPT))

        await asyncio.wait_for(self.loop.tick(), WAIT)

        self.assertEqual(self.builds, builds)
        self.assertEqual(await self.left_in_queue(), [])

    async def test_an_interrupted_intake_build_leaves_an_empty_list(self):
        """Задачи по только что присланному файлу собираются ДО того, как прайс попал в
        список, — и прервать хочется именно такую сборку (`/stop` в Telegram)."""
        self.hang = True
        self.started.clear()
        intake = asyncio.ensure_future(self.model.submit(
            XLSX, "Прайс Монарх 20.09.2026.xlsx", supplier_hint="Монарх", actor="admin-1"))
        await asyncio.wait_for(self.started.wait(), WAIT)

        fresh = max(self.model._running)                    # номер, который ещё не в списке
        self.assertTrue(self.model.interrupt(fresh))
        await asyncio.wait_for(intake, WAIT)

        price = self.model.price(fresh)
        self.assertIsNotNone(price, "прайс принят, прервана только сборка")
        self.assertEqual(price.tasks, [])
        self.assertIn("/rebuild", self.seen.texts())

    # ------------------------------------------------------------------ задача

    async def test_a_hung_task_goes_back_to_todo(self):
        task = self.price.sorted_tasks[0]
        await self.interrupt_midway(CommandKind.EXECUTE_TASK, task_id=task.id)

        self.assertEqual(task.status, TaskStatus.TODO)
        self.assertIn("Прервано админом", task.result)
        self.assertIn("проверьте в 1С", task.result,
                      "запись, ушедшая до прерывания, осталась — админ обязан это знать")

    async def test_an_interrupted_task_can_no_longer_write(self):
        """Обращение к 1С, ушедшее в поток до отмены, ещё может дойти до `guard`, — и обязано
        получить отказ, а не продлённую аренду."""
        task = self.price.sorted_tasks[0]
        await self.interrupt_midway(CommandKind.EXECUTE_TASK, task_id=task.id)

        self.assertIsNone(self.model.lock_of(self.price.id))
        with self.assertRaises(WriteRefused):
            self.guard()

    async def test_the_task_survives_a_restart_as_todo(self):
        task = self.price.sorted_tasks[0]
        await self.interrupt_midway(CommandKind.EXECUTE_TASK, task_id=task.id)

        again = (await self.model_store.load_all())[0]
        stored = next(t for t in again.tasks if t.id == task.id)
        self.assertEqual(stored.status, TaskStatus.TODO)

    # ------------------------------------------------------------------ цикл

    async def test_other_commands_wait_for_the_end_of_the_run(self):
        """Во время прогона цикл берёт ТОЛЬКО прерывания: остальные команды ждут конца, как
        и раньше, — порядок «кто первый» и захваты от сторожа не меняются."""
        first, second = self.price.sorted_tasks[:2]
        await self.queue.put(self.command(CommandKind.EXECUTE_TASK, task_id=first.id))
        tick = asyncio.ensure_future(self.loop.tick())
        await asyncio.wait_for(self.started.wait(), WAIT)

        await self.queue.put(self.command(CommandKind.EDIT_TASK_DESCRIPTION,
                                          task_id=second.id, payload={"description": "x"}))
        await asyncio.sleep(0.1)                    # несколько оборотов сторожа
        self.assertEqual([c.task_id for c in await self.queue.pending()], [second.id],
                         "правку описания сторож не трогает")

        await self.queue.put(self.command(CommandKind.INTERRUPT))
        await asyncio.wait_for(tick, WAIT)
        self.assertEqual([c.task_id for c in await self.queue.pending()], [second.id],
                         "она дождётся следующего оборота")

    async def test_nothing_to_interrupt_is_not_an_error(self):
        self.seen.events.clear()
        await self.queue.put(self.command(CommandKind.INTERRUPT))
        await asyncio.wait_for(self.loop.tick(), WAIT)

        self.assertNotIn(EventKind.COMMAND_REJECTED, self.seen.kinds())
        self.assertEqual(await self.left_in_queue(), [])

    async def test_stopping_the_process_is_not_swallowed(self):
        """Отмену, присланную НЕ админом, — остановку процесса — глотать нельзя: иначе бот не
        остановился бы, пока идёт задача."""
        task = self.price.sorted_tasks[0]
        await self.queue.put(self.command(CommandKind.EXECUTE_TASK, task_id=task.id))
        tick = asyncio.ensure_future(self.loop.tick())
        await asyncio.wait_for(self.started.wait(), WAIT)

        tick.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(tick, WAIT)
        self.assertNotIn("Прервано админом", task.result or "")


class ProgressTest(unittest.IsolatedAsyncioTestCase):
    """Пачка задач докладывается в 1С ПО МЕРЕ выполнения, а не разом после последней
    (вопрос админа 08.10.2026)."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "t.db"
        store = ModelStore(self.db)
        await store.init()
        suppliers = SupplierStore(self.db)
        await suppliers.init()
        self.queue = CommandQueue(self.db)
        await self.queue.init()

        async def run(price, task, content, guard):
            return TaskStatus.DONE, "готово"

        self.model = PriceListService(
            store, suppliers,
            save_file=lambda content, name: price_files.save(self.db, name, content),
            run_task=run)
        await self.model.load()
        await self.model.submit(XLSX, "Прайс Монарх 15.09.2026.xlsx", supplier_hint="Монарх",
                                actor="admin-1")
        self.price = self.model.prices[0]

        test = self

        class Provider:
            """Что видел бы провайдер 1С при каждом опросе: статусы задач и очередь."""
            def __init__(self):
                self.seen = []

            async def collect(self, queue):
                alive = {c.id for c in await queue.pending() + await queue.taken()}
                self.seen.append(([t.status for t in test.tasks], alive))

        self.provider = Provider()
        self.loop = AgentLoop(self.queue, self.model, providers=[self.provider])

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_each_task_is_reported_as_soon_as_it_is_done(self):
        self.tasks = self.price.sorted_tasks[:2]
        first, second = [await self.queue.put(Command(
            kind=CommandKind.EXECUTE_TASK, source="1c", actor="admin-1",
            price_id=self.price.id, task_id=t.id, payload={"seq": n}))
            for n, t in enumerate(self.tasks, 1)]

        await asyncio.wait_for(self.loop.tick(), WAIT)

        midway = [(statuses, alive) for statuses, alive in self.provider.seen
                  if statuses == [TaskStatus.DONE, TaskStatus.TODO]]
        self.assertTrue(midway, "между задачами провайдер не опрашивался — статус первой "
                                "уехал бы в 1С только вместе со второй")
        _, alive = midway[0]
        self.assertNotIn(first.id, alive, "команда первой задачи уже закрыта — 1С погасит "
                                          "её колесико")
        self.assertIn(second.id, alive)


if __name__ == "__main__":
    unittest.main()
