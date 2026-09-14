"""Сквозной прогон модели: приём прайса → задачи → команды → цикл (§4–§9).

Проверяется вся обвязка на настоящих объектах: файл, справочники, база, очередь, цикл.
Выполнение задачи — заглушка, в 1С ничего не пишется; проверяется именно ОБВЯЗКА, ради
которой всё и собиралось.
"""
import tempfile
import unittest
from pathlib import Path

from src.bot.model_loop import AgentLoop
from src.model.commands import Command, CommandKind
from src.model.enums import PriceStatus, TaskKind, TaskStatus
from src.model.events import Broadcaster, Listener
from src.model.service import PriceListService
from src.price_tool import model_view as view
from src.storage import price_files
from src.storage.command_queue import CommandQueue
from src.storage.model_store import ModelStore
from src.storage.suppliers import SupplierStore

XLSX = b"PK\x03\x04 not a real workbook"


class Collector(Listener):
    def __init__(self):
        self.events = []

    async def notify(self, event):
        self.events.append(event)

    def kinds(self):
        return [e.kind for e in self.events]

    def texts(self):
        return "\n".join(e.text for e in self.events)


class Base(unittest.IsolatedAsyncioTestCase):

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

        self.model = PriceListService(
            self.model_store, self.suppliers,
            save_file=lambda content, name: price_files.save(self.db, name, content),
            broadcaster=bus)
        await self.model.load()
        self.loop = AgentLoop(self.queue, self.model)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def submit(self, name="Прайс Монарх 15.09.2026.xlsx", hint="Монарх", **kw):
        return await self.model.submit(XLSX, name, supplier_hint=hint,
                                       actor="admin-1", **kw)

    async def send(self, kind, **kw):
        await self.queue.put(Command(kind=kind, source="telegram", actor="admin-1", **kw))
        return await self.loop.tick()


class IntakeTest(Base):

    async def test_price_appears_with_stub_tasks(self):
        self.assertEqual(await self.submit(), "")
        prices = self.model.prices
        self.assertEqual(len(prices), 1)
        self.assertTrue(prices[0].tasks)
        self.assertEqual(prices[0].status, PriceStatus.TODO)

    async def test_supplier_and_signature_are_recorded(self):
        """Вывод об опознании закрепляется в справочнике (§2.2)."""
        await self.submit()
        suppliers = await self.suppliers.list_suppliers()
        self.assertEqual([s.name for s in suppliers], ["Монарх"])
        self.assertEqual(len(await self.suppliers.list_signatures(suppliers[0].id)), 1)
        self.assertEqual(len(await self.suppliers.list_price_files()), 1)

    async def test_file_is_saved_and_referenced(self):
        await self.submit()
        path = self.model.prices[0].supplier_price.file_path
        self.assertTrue(Path(path).is_file())
        self.assertIn(path, await self.model_store.known_paths())

    async def test_caption_names_the_supplier(self):
        await self.submit(hint="Most Flooring")
        self.assertEqual([s.name for s in await self.suppliers.list_suppliers()],
                         ["Most Flooring"])

    async def test_without_caption_name_comes_from_the_file(self):
        await self.submit(name="Линдервуд.xlsx", hint="")
        self.assertEqual([s.name for s in await self.suppliers.list_suppliers()],
                         ["Линдервуд"])

    async def test_known_signature_identifies_the_supplier(self):
        """Сигнатуру уже видели — второй файл того же формата берёт её владельца."""
        await self.submit(name="Монарх 01.09.xlsx", hint="Монарх")
        await self.submit(name="совсем другое имя.xlsx", hint="", force=True)
        self.assertEqual(len(await self.suppliers.list_suppliers()), 1)

    async def test_outdated_price_is_rejected_and_its_file_removed(self):
        """От отклонённого не остаётся ничего — ни файла, ни записи о нём (§4)."""
        await self.submit(name="Монарх 15.09.2026.xlsx")
        before = len(await self.suppliers.list_price_files())

        reason = await self.submit(name="Монарх 01.09.2026.xlsx")

        self.assertIn("более свежий", reason)
        self.assertEqual(len(self.model.prices), 1)
        self.assertEqual(len(await self.suppliers.list_price_files()), before)

    async def test_supplier_survives_a_rejected_price(self):
        """Поставщик и сигнатура про опознание, а не про прайс — они остаются."""
        await self.submit(name="Монарх 15.09.2026.xlsx")
        await self.submit(name="Монарх 01.09.2026.xlsx")
        self.assertEqual(len(await self.suppliers.list_suppliers()), 1)

    async def test_force_takes_the_outdated_one_and_marks_it(self):
        await self.submit(name="Монарх 15.09.2026.xlsx")
        await self.submit(name="Монарх 01.09.2026.xlsx", force=True)

        prices = {p.supplier_price.filename: p for p in self.model.prices}
        old = prices["Монарх 01.09.2026.xlsx"]
        self.assertTrue(old.has_newer)
        self.assertTrue(Path(old.supplier_price.file_path).is_file())


class CommandFlowTest(Base):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.submit()
        self.price = self.model.prices[0]
        self.task = self.price.sorted_tasks[0]
        self.seen.events.clear()

    async def test_run_marks_the_task_and_says_it_is_a_stub(self):
        await self.send(CommandKind.EXECUTE_TASK, price_id=self.price.id,
                        task_id=self.task.id)
        self.assertEqual(self.task.status, TaskStatus.DONE)
        self.assertIn("ЗАГЛУШКА", self.task.result)
        self.assertIn("в 1С ничего не записано", self.task.result)

    async def test_run_survives_reload(self):
        await self.send(CommandKind.EXECUTE_TASK, price_id=self.price.id,
                        task_id=self.task.id)
        again = (await self.model_store.load_all())[0]
        self.assertEqual(again.task_by_id(self.task.id).status, TaskStatus.DONE)

    async def test_run_takes_the_lock_and_holds_it_after(self):
        """После задачи захват держится ещё минуту (§5.1)."""
        await self.send(CommandKind.EXECUTE_TASK, price_id=self.price.id,
                        task_id=self.task.id)
        lock = self.model.lock_of(self.price.id)
        self.assertIsNotNone(lock)
        self.assertFalse(lock.working)
        self.assertEqual(lock.actor, "admin-1")

    async def test_another_admin_is_refused_while_locked(self):
        await self.send(CommandKind.EXECUTE_TASK, price_id=self.price.id,
                        task_id=self.task.id)
        other = self.price.sorted_tasks[1]
        await self.queue.put(Command(kind=CommandKind.EXECUTE_TASK, source="telegram",
                                     actor="admin-2", price_id=self.price.id,
                                     task_id=other.id))
        self.seen.events.clear()
        await self.loop.tick()

        self.assertEqual(other.status, TaskStatus.TODO)
        self.assertIn("занят", self.seen.texts())

    async def test_holder_is_not_blocked_by_his_own_lock(self):
        await self.send(CommandKind.EXECUTE_TASK, price_id=self.price.id,
                        task_id=self.task.id)
        other = self.price.sorted_tasks[1]
        await self.send(CommandKind.EXECUTE_TASK, price_id=self.price.id,
                        task_id=other.id)
        self.assertEqual(other.status, TaskStatus.DONE)

    async def test_edit_then_run_in_one_batch_carries_the_text(self):
        """Сценарий админа: правит описание и тут же жмёт «Выполнить» (§7)."""
        await self.queue.put(Command(kind=CommandKind.EDIT_TASK_DESCRIPTION,
                                     actor="admin-1", price_id=self.price.id,
                                     task_id=self.task.id,
                                     payload={"text": "сверить только размеры"}))
        await self.queue.put(Command(kind=CommandKind.EXECUTE_TASK, actor="admin-1",
                                     price_id=self.price.id, task_id=self.task.id))

        await self.loop.tick()

        self.assertEqual(self.task.description, "сверить только размеры")
        self.assertEqual(self.task.status, TaskStatus.DONE)

    async def test_status_is_set_by_admin(self):
        await self.send(CommandKind.SET_TASK_STATUS, price_id=self.price.id,
                        task_id=self.task.id, payload={"status": "частично обработана"})
        self.assertEqual(self.task.status, TaskStatus.PARTIAL)

    async def test_unknown_status_is_refused(self):
        await self.send(CommandKind.SET_TASK_STATUS, price_id=self.price.id,
                        task_id=self.task.id, payload={"status": "готово"})
        self.assertEqual(self.task.status, TaskStatus.TODO)
        self.assertIn("неизвестный статус", self.seen.texts())

    async def test_task_deleted(self):
        await self.send(CommandKind.DELETE_TASK, price_id=self.price.id,
                        task_id=self.task.id)
        self.assertIsNone(self.price.task_by_id(self.task.id))

    async def test_price_status_is_set_by_admin_not_by_readiness(self):
        for task in list(self.price.tasks):
            task.complete(TaskStatus.DONE, "ок")
        self.assertTrue(self.price.ready)
        self.assertEqual(self.price.status, PriceStatus.TODO)

        await self.send(CommandKind.SET_PRICE_STATUS, price_id=self.price.id,
                        payload={"status": "выполнен"})
        self.assertEqual(self.price.status, PriceStatus.DONE)

    async def test_rebuild_drops_statuses_and_gives_new_ids(self):
        old_id = self.task.id
        self.task.complete(TaskStatus.DONE, "сделано")
        await self.model_store.update_task(self.task)

        await self.send(CommandKind.REBUILD_TASKS, price_id=self.price.id)

        self.assertTrue(self.price.tasks)
        self.assertTrue(all(t.status == TaskStatus.TODO for t in self.price.tasks))
        self.assertNotIn(old_id, [t.id for t in self.price.tasks])

    async def test_destroy_removes_price_with_tasks(self):
        await self.send(CommandKind.DESTROY_PRICE, price_id=self.price.id)
        self.assertEqual(self.model.prices, [])
        self.assertEqual(await self.model_store.load_all(), [])

    async def test_unlock_releases_own_lock(self):
        await self.send(CommandKind.EXECUTE_TASK, price_id=self.price.id,
                        task_id=self.task.id)
        await self.send(CommandKind.RELEASE_LOCK, price_id=self.price.id)
        self.assertIsNone(self.model.lock_of(self.price.id))

    async def test_unlock_of_another_admin_is_refused(self):
        """Чужой захват не снять. Отказ приходит ещё на входе — командам по занятому
        прайсу дальше разбора пачки хода нет (§7), и до `_release` дело не доходит."""
        await self.send(CommandKind.EXECUTE_TASK, price_id=self.price.id,
                        task_id=self.task.id)
        await self.queue.put(Command(kind=CommandKind.RELEASE_LOCK, actor="admin-2",
                                     price_id=self.price.id))
        self.seen.events.clear()
        await self.loop.tick()
        self.assertIsNotNone(self.model.lock_of(self.price.id))
        self.assertIn("занят", self.seen.texts())

    async def test_command_for_a_missing_task_is_refused_not_silent(self):
        await self.send(CommandKind.EXECUTE_TASK, price_id=self.price.id, task_id=9999)
        self.assertIn("задача не найдена", self.seen.texts())

    async def test_queue_is_emptied_after_the_tick(self):
        await self.send(CommandKind.EXECUTE_TASK, price_id=self.price.id,
                        task_id=self.task.id)
        self.assertEqual(await self.queue.pending(), [])
        self.assertEqual(await self.queue.taken(), [])


class ViewTest(Base):

    async def test_empty_list_explains_what_to_do(self):
        text = view.render_prices([])
        self.assertIn("пуст", text)
        self.assertIn("/model_force", text)

    async def test_price_list_shows_counts_and_lock(self):
        await self.submit()
        price = self.model.prices[0]
        price.sorted_tasks[0].complete(TaskStatus.DONE, "ок")

        text = view.render_prices(self.model.prices, {}, {price.supplier_price.supplier_id:
                                                          "Монарх"})
        self.assertIn(f"№{price.id}", text)
        self.assertIn("Монарх", text)
        # Числу предшествует слово, которое оно считает: «задач 0/5» админ прочитал как
        # «ноль задач», и формат был в этом виноват, а не он.
        self.assertIn(f"задач {len(price.tasks)}", text)
        self.assertIn("выполнено 1", text)

    async def test_tasks_are_grouped_by_kind_in_spec_order(self):
        await self.submit()
        text = view.render_tasks(self.model.prices[0], "Монарх")
        self.assertLess(text.index(TaskKind.NORMALIZE_NAMES.value),
                        text.index(TaskKind.CHANGE_PRICES.value))
        self.assertLess(text.index(TaskKind.MOVE_DISCONTINUED.value),
                        text.index(TaskKind.ADD_NEW.value))

    async def test_outdated_price_is_marked_in_both_views(self):
        await self.submit(name="Монарх 15.09.2026.xlsx")
        await self.submit(name="Монарх 01.09.2026.xlsx", force=True)
        old = next(p for p in self.model.prices if p.has_newer)

        self.assertIn("устарел", view.render_prices(self.model.prices))
        self.assertIn("Устарел", view.render_tasks(old))


if __name__ == "__main__":
    unittest.main()
