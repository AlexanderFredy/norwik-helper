"""Выполнение задачи с настоящей записью в 1С (§6.2 specs/agent-workflow-model.md).

Клиент 1С и оркестратор поддельные: проверяется не то, что 1С поймёт payload — это
проверяет прайсовый поток, — а обвязка вокруг записи, которую живой базой как раз
проверить нельзя без порчи данных.

ГЛАВНОЕ ЗДЕСЬ — ЗАЩИТА ЗАПИСИ. Проверка права стоит вплотную перед вызовом 1С, потому что
необратима именно запись: статус мы поправим, а цены в справочнике нет.
"""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from src.model.enums import TaskKind, TaskStatus
from src.model.events import Broadcaster
from src.model.executor import TaskTools, WriteRefused, run, task_brief
from src.model.price import Price, SupplierPrice
from src.model.refs import Ref, TaskAddress
from src.model.task import PriceTask
from src.onec.client import NomItem, Price as OnecPrice
from src.storage import price_files
from src.storage.model_store import ModelStore
from src.storage.suppliers import SupplierStore


def nom(ref="T1", article="A1", name="Дуб Верона", collection="Vintage",
        purchase="1000", unit="м2"):
    return NomItem(
        ref=ref, id="", name=name, article=article, unit=unit, size="",
        product_type="Ламинат", collection=collection, parent=collection,
        collection_ref="F1", alt_units={},
        purchase=OnecPrice(value=float(purchase), date=None),
        retail=None, rrc=None, product_type_ref="PT1")


class FakeNomenclature:
    def __init__(self, items, tm=""):
        self.items = list(items)
        self.tm = tm
        self.errors = []


class FakeOnec:
    """Считает записи и отдаёт заданную номенклатуру."""

    def __init__(self, items=None, tm_name="Most Flooring"):
        self._items = list(items or [nom()])
        self.tm_name = tm_name
        self.price_writes = []
        self.item_writes = []

    def by_tm_all(self, tm_code, **kw):
        return FakeNomenclature(self._items, tm=self.tm_name)

    def set_prices(self, payload):
        self.price_writes.append(payload)
        return {"updated": len(payload), "unchanged": 0, "errors": []}

    def set_items(self, ops):
        self.item_writes.append(ops)
        return {"created": 0, "updated": len(ops), "errors": []}


def make_task(kind=TaskKind.CHANGE_PRICES, description="обновить цены"):
    return PriceTask(kind=kind,
                     address=TaskAddress(tm=Ref.make(code="TM1", names=["Egger"]),
                                         subject=Ref.make(names=["Vintage"])),
                     description=description, id=7)


def make_price():
    return Price(supplier_price=SupplierPrice(supplier_id=1, file_id=1,
                                              file_path="p.xlsx",
                                              filename="Прайс.xlsx"), id=3)


def allow():
    """Право на запись есть."""
    return None


def deny():
    raise WriteRefused("захват потерян")


class WriteGuardTest(unittest.IsolatedAsyncioTestCase):
    """Никакая запись не проходит без свежей проверки права."""

    async def test_prices_are_written_when_allowed(self):
        onec = FakeOnec()
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PRICES)
        out = await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1500})
        self.assertTrue(onec.price_writes, "цены обязаны уйти в 1С: %s" % out)
        self.assertEqual(tools.written_prices, 1)

    async def test_prices_are_not_written_when_refused(self):
        onec = FakeOnec()
        tools = TaskTools(onec, b"", "p.xlsx", deny, kind=TaskKind.CHANGE_PRICES)
        out = await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1500})
        self.assertEqual(onec.price_writes, [], "запись прошла без права")
        self.assertIn("ЗАПИСЬ ОТМЕНЕНА", out)

    async def test_items_are_not_written_when_refused(self):
        onec = FakeOnec()
        tools = TaskTools(onec, b"", "p.xlsx", deny, kind=TaskKind.ADD_NEW)
        await tools.execute("write_items", {
            "tm_code": "TM1", "product_type": "PT1", "collection": "Vintage",
            "items": [{"op": "update", "ref": "T1", "title": "Дуб Медовый"}]})
        self.assertEqual(onec.item_writes, [], "запись прошла без права")

    async def test_guard_is_checked_after_the_plan_not_before(self):
        """Право проверяется ВПЛОТНУЮ перед записью.

        Сборка плана ходит в 1С за выгрузкой и занимает время; проверив право до неё, мы
        бы разрешили запись по праву, которого к моменту записи уже нет.
        """
        onec = FakeOnec()
        seen = []

        def watching():
            seen.append(len(onec.price_writes))

        tools = TaskTools(onec, b"", "p.xlsx", watching, kind=TaskKind.CHANGE_PRICES)
        await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1500})
        # проверка была ровно одна и ДО записи
        self.assertEqual(seen, [0])
        self.assertEqual(len(onec.price_writes), 1)

    async def test_nothing_to_write_does_not_touch_1c(self):
        """Совпадающая цена не должна порождать запись: 2% порог держит `plan_collection`."""
        onec = FakeOnec()
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PRICES)
        out = await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1000})
        self.assertEqual(onec.price_writes, [])
        self.assertIn("Изменений нет", out)

    async def test_unknown_collection_is_refused_not_guessed(self):
        onec = FakeOnec()
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PRICES)
        out = await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Такой нет", "purchase": 1500})
        self.assertEqual(onec.price_writes, [])
        self.assertIn("нет коллекции", out)


class BrandNameTest(unittest.IsolatedAsyncioTestCase):
    """Имя марки в наименованиях берётся из 1С, а не из того, что прислала модель.

    ЧТО ЭТО ЛОВИТ. `build_name` собирает имя из частей, и марка была среди них. Модель,
    увидев в прайсе «MOST FLOOR», честно передавала это написание — и нормализация
    переименовывала сотни позиций. Запретом в промпте не лечится: модель копирует не по
    злому умыслу, а потому что так написано в источнике.
    """

    async def _written_name(self, onec, tm_name):
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.NORMALIZE_NAMES)
        await tools.execute("write_items", {
            "tm_code": "TM1", "tm_name": tm_name,
            "product_type": "PT1", "product_type_name": "Ламинат",
            "collection": "Vintage",
            "items": [{"op": "update", "ref": "T1", "title": "Дуб Медовый"}]})
        if not onec.item_writes:
            return ""
        ops = onec.item_writes[0]
        return next((o.get("name", "") for o in ops if o.get("name")), "")

    async def test_the_1c_spelling_wins(self):
        onec = FakeOnec(tm_name="Most Flooring")
        name = await self._written_name(onec, "MOST FLOOR")
        self.assertIn("Most Flooring", name)
        self.assertNotIn("MOST FLOOR", name)

    async def test_divergence_is_reported_not_swallowed(self):
        """Переименование марки — решение админа, и он узнает о расхождении только так."""
        onec = FakeOnec(tm_name="Most Flooring")
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.NORMALIZE_NAMES)
        out = await tools.execute("write_items", {
            "tm_code": "TM1", "tm_name": "MOST FLOOR",
            "product_type": "PT1", "product_type_name": "Ламинат",
            "collection": "Vintage",
            "items": [{"op": "update", "ref": "T1", "title": "Дуб Медовый"}]})
        self.assertIn("Most Flooring", out)
        self.assertIn("MOST FLOOR", out)

    async def test_matching_spelling_says_nothing(self):
        onec = FakeOnec(tm_name="Most Flooring")
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.NORMALIZE_NAMES)
        out = await tools.execute("write_items", {
            "tm_code": "TM1", "tm_name": "most flooring",
            "product_type": "PT1", "product_type_name": "Ламинат",
            "collection": "Vintage",
            "items": [{"op": "update", "ref": "T1", "title": "Дуб Медовый"}]})
        self.assertNotIn("⚠️ Марка", out)


class OutcomeTest(unittest.IsolatedAsyncioTestCase):

    async def test_finish_records_the_outcome(self):
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow)
        await tools.execute("finish", {"status": "выполнена",
                                       "result": "Записано 12 цен."})
        self.assertEqual(tools.outcome, (TaskStatus.DONE, "Записано 12 цен."))

    async def test_partial_without_a_reason_is_refused(self):
        """«Частично» без причины — бесполезный ответ: админ не узнает, что доделывать."""
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow)
        out = await tools.execute("finish", {"status": "частично обработана",
                                             "result": "не всё"})
        self.assertIsNone(tools.outcome)
        self.assertIn("требует причины", out)

    async def test_empty_result_is_refused(self):
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow)
        await tools.execute("finish", {"status": "выполнена", "result": "   "})
        self.assertIsNone(tools.outcome)

    async def test_unknown_status_is_refused_with_the_list(self):
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow)
        out = await tools.execute("finish", {"status": "готово", "result": "всё ок"})
        self.assertIsNone(tools.outcome)
        self.assertIn("к обработке", out)


class SilentAgentTest(unittest.IsolatedAsyncioTestCase):
    """Молчаливый агент не имеет права выдать себя за успех."""

    class Orchestrator:
        def __init__(self, calls=()):
            self._calls = list(calls)

        async def handle_turn(self, history, system=None, extra_tools=None,
                              extra_executor=None, **kw):
            for name, payload in self._calls:
                await extra_executor.execute(name, payload)
            return "молчу", history

    async def test_no_finish_and_no_write_leaves_the_task_open(self):
        status, result = await run(self.Orchestrator(), FakeOnec(), make_price(),
                                   make_task(), b"", allow)
        self.assertEqual(status, TaskStatus.TODO)
        self.assertIn("не доложил исход", result)

    async def test_no_finish_but_a_write_is_partial_not_open(self):
        """Прятать состоявшуюся запись за «к обработке» нельзя: админ повторит задачу и
        запишет второй раз."""
        onec = FakeOnec()
        orc = self.Orchestrator([
            ("write_prices", {"tm_code": "TM1", "collection": "Vintage",
                              "purchase": 1500}),
        ])
        status, result = await run(orc, onec, make_price(), make_task(), b"", allow)
        self.assertEqual(status, TaskStatus.PARTIAL)
        self.assertIn("запись в 1С состоялась", result)
        self.assertTrue(onec.price_writes)

    async def test_finish_wins_over_the_fallback(self):
        orc = self.Orchestrator([
            ("finish", {"status": "выполнена", "result": "Обновлено 5 цен в коллекции."}),
        ])
        status, result = await run(orc, FakeOnec(), make_price(), make_task(), b"", allow)
        self.assertEqual(status, TaskStatus.DONE)
        self.assertEqual(result, "Обновлено 5 цен в коллекции.")


class AdminEditTest(unittest.TestCase):
    """Правка админа старше рассуждений агента, и он должен это знать.

    Описание уезжает одним куском, и отличить «анализ агента» от «ответа админа» по
    структуре нельзя — поля разные у них нет. Значит правило должно быть в промпте:
    иначе агент читает свой же вопрос, не замечает ответа ниже и решает заново сам.
    """

    def test_prompt_names_the_admin_edit_authoritative(self):
        from src.model.executor import PROMPT
        self.assertIn("его слова старше твоих", PROMPT)
        self.assertIn("действуй по ОТВЕТУ", PROMPT)

    def test_prompt_says_he_cannot_ask_mid_run(self):
        """Ключевое ограничение: спросить посреди работы нельзя, только `finish`."""
        from src.model.executor import PROMPT
        self.assertIn("Переспросить посреди работы ты не можешь", PROMPT)


class BriefTest(unittest.TestCase):

    def test_brief_carries_what_the_agent_needs(self):
        task = make_task(description="сверить РРЦ")
        brief = task_brief(make_price(), task)
        self.assertIn("изменение цен", brief)
        self.assertIn("сверить РРЦ", brief)
        self.assertIn("TM1", brief)
        self.assertIn("Прайс.xlsx", brief)

    def test_previous_result_is_included_on_a_rerun(self):
        """Задачу запускают повторно именно тогда, когда в прошлый раз получилось не всё:
        агент должен доделать остаток, а не начать сначала."""
        task = make_task()
        task.complete(TaskStatus.PARTIAL, "12 из 15 записано, по трём нет позиций")
        self.assertIn("12 из 15", task_brief(make_price(), task))


class PayloadSizeTest(unittest.IsolatedAsyncioTestCase):
    """Объём выгрузки зависит от вида задачи — история едет в каждый следующий запрос."""

    async def test_price_task_gets_no_catalog_fields(self):
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PRICES)
        out = await tools.execute("get_1c_items", {"tm_code": "TM1"})
        self.assertIn("purchase", out)
        self.assertNotIn("site_name", out)
        self.assertNotIn("properties", out)

    async def test_catalog_task_gets_them(self):
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow,
                          kind=TaskKind.NORMALIZE_NAMES)
        out = await tools.execute("get_1c_items", {"tm_code": "TM1"})
        self.assertIn("site_name", out)
        self.assertIn("not_exported", out)


class ServiceWiringTest(unittest.IsolatedAsyncioTestCase):
    """Модель зовёт исполнитель и сохраняет его исход, а не свой."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        db = Path(self._dir.name) / "t.db"
        self.store = ModelStore(db)
        await self.store.init()
        self.suppliers = SupplierStore(db)
        await self.suppliers.init()
        self.db = db

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def build(self, run_task):
        from src.model.service import PriceListService
        model = PriceListService(
            self.store, self.suppliers,
            save_file=lambda c, n: price_files.save(self.db, n, c),
            broadcaster=Broadcaster(), run_task=run_task)
        await model.load()
        return model

    async def test_outcome_from_the_agent_is_stored(self):
        seen = {}

        async def runner(price, task, content, guard):
            seen["task"] = task.id
            return TaskStatus.PARTIAL, "Записано 3 из 5, по двум нет артикула."

        model = await self.build(runner)
        from tests.test_task_builder import workbook
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")
        price = model.prices[0]
        task = price.sorted_tasks[0]

        from src.model.commands import Command, CommandKind
        await model.apply(Command(kind=CommandKind.EXECUTE_TASK, actor="admin-1",
                                  price_id=price.id, task_id=task.id))

        self.assertEqual(seen["task"], task.id)
        self.assertEqual(task.status, TaskStatus.PARTIAL)
        self.assertIn("3 из 5", task.result)

    async def test_a_crashed_run_leaves_the_task_open(self):
        """Сорвавшийся прогон мог успеть записать часть — выдавать это за успех нельзя."""
        async def runner(price, task, content, guard):
            raise RuntimeError("1С недоступна")

        model = await self.build(runner)
        from tests.test_task_builder import workbook
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")
        price = model.prices[0]
        task = price.sorted_tasks[0]

        from src.model.commands import Command, CommandKind
        await model.apply(Command(kind=CommandKind.EXECUTE_TASK, actor="admin-1",
                                  price_id=price.id, task_id=task.id))

        self.assertEqual(task.status, TaskStatus.TODO)
        self.assertIn("проверьте в 1С", task.result)

    async def test_guard_refuses_after_the_lock_is_gone(self):
        """Сердцевина защиты: захват сняли посреди прогона — запись обязана отвалиться."""
        captured = {}

        async def runner(price, task, content, guard):
            guard()                                  # право ещё есть
            model._locks.pop(price.id, None)         # захват сняли
            try:
                guard()
                captured["refused"] = False
            except WriteRefused:
                captured["refused"] = True
            return TaskStatus.TODO, "проверка"

        model = await self.build(runner)
        from tests.test_task_builder import workbook
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")
        price = model.prices[0]
        task = price.sorted_tasks[0]

        from src.model.commands import Command, CommandKind
        await model.apply(Command(kind=CommandKind.EXECUTE_TASK, actor="admin-1",
                                  price_id=price.id, task_id=task.id))
        self.assertTrue(captured.get("refused"), "запись разрешена без захвата")

    async def test_guard_refuses_a_relocked_price(self):
        """Самый коварный случай: захват сняли и тут же взяли ЗАНОВО другим прогоном.

        Актор и живость совпадают — различает только поколение.
        """
        captured = {}

        async def runner(price, task, content, guard):
            import src.model.locks as lk
            old = model._locks[price.id]
            model._locks[price.id] = lk.acquire(old, price.id, "admin-1")
            try:
                guard()
                captured["refused"] = False
            except WriteRefused:
                captured["refused"] = True
            return TaskStatus.TODO, "проверка"

        model = await self.build(runner)
        from tests.test_task_builder import workbook
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")
        price = model.prices[0]
        task = price.sorted_tasks[0]

        from src.model.commands import Command, CommandKind
        await model.apply(Command(kind=CommandKind.EXECUTE_TASK, actor="admin-1",
                                  price_id=price.id, task_id=task.id))
        self.assertTrue(captured.get("refused"), "запись разрешена по чужому поколению")

    async def test_without_a_runner_it_stays_a_stub(self):
        model = await self.build(None)
        from tests.test_task_builder import workbook
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")
        price = model.prices[0]
        task = price.sorted_tasks[0]

        from src.model.commands import Command, CommandKind
        await model.apply(Command(kind=CommandKind.EXECUTE_TASK, actor="admin-1",
                                  price_id=price.id, task_id=task.id))
        self.assertIn("ЗАГЛУШКА", task.result)


if __name__ == "__main__":
    unittest.main()
