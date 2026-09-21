"""Формирование задач агентом (§6.1 модели).

Проверяются инструменты и обвязка — без сети: оркестратор поддельный. Смысл в том, что
задачи должны получаться на РЕАЛЬНЫХ парах (марка, коллекция) из прайса, а не по пять
одинаковых строк на лист, как делала заглушка.
"""
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from src.model.enums import TaskKind, TaskSubject
from src.model.events import Broadcaster
from src.model.service import PriceListService
from src.model.task_builder import TaskBuilderTools, build
from src.storage import price_files
from src.storage.model_store import ModelStore
from src.storage.suppliers import SupplierStore


def workbook() -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "Ламинат"
    sheet.append(["Артикул", "Коллекция", "Цена"])
    sheet.append(["LE-263", "Vintage", 1290])
    second = wb.create_sheet("Плитка")
    second.append(["Артикул", "Коллекция", "Цена"])
    second.append(["A001", "Adventure", 2450])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


class FakeOnec:
    def selling_tm(self, all_marks=False):
        from src.onec.client import TradeMark
        return [TradeMark(name="Egger", code="T1"),
                TradeMark(name="A+ Floor", code="T9", selling=False)]


class ToolsTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tools = TaskBuilderTools(workbook(), "Прайс.xlsx", onec=FakeOnec())

    async def test_read_shows_sheets_and_rows(self):
        out = await self.tools.execute("read_price", {})
        self.assertIn("Ламинат", out)
        self.assertIn("Плитка", out)
        self.assertIn("Vintage", out)

    async def test_read_picks_the_named_sheet(self):
        out = await self.tools.execute("read_price", {"sheet": "Плитка"})
        self.assertIn("Adventure", out)

    async def test_unknown_sheet_falls_back_to_the_first(self):
        out = await self.tools.execute("read_price", {"sheet": "нет такого"})
        self.assertIn("Vintage", out)

    async def test_unparsable_file_says_so_instead_of_failing(self):
        tools = TaskBuilderTools(b"not a table at all", "битый.xlsx")
        self.assertIn("не разобрался", await tools.execute("read_price", {}))

    async def test_trade_marks_include_unexported(self):
        marks = json.loads(await self.tools.execute("get_selling_tm", {}))
        self.assertEqual([m["name"] for m in marks], ["Egger", "A+ Floor"])
        self.assertFalse(marks[1]["selling"])

    async def test_task_is_collected_with_real_address(self):
        await self.tools.execute("add_task", {
            "kind": "изменение цен", "tm": "Egger", "tm_code": "T1",
            "collection": "Vintage", "description": "в прайсе есть колонка цены"})
        task = self.tools.collected[0]
        self.assertEqual(task.kind, TaskKind.CHANGE_PRICES)
        self.assertEqual(task.address.tm.code, "T1")
        self.assertIn("Vintage", task.address.subject.names)

    async def test_item_task_carries_the_article(self):
        await self.tools.execute("add_task", {
            "kind": "изменение свойств", "tm": "Egger", "item": "Дуб Верона",
            "article": "LE-263", "description": "в прайсе указан класс 32"})
        task = self.tools.collected[0]
        self.assertEqual(task.subject, TaskSubject.ITEM)
        self.assertEqual(task.address.subject.article, "le263")

    async def test_same_address_and_kind_adds_to_description(self):
        """Модель может прийти к той же задаче дважды — это дополнение, не вторая (§3.3)."""
        for text in ("есть колонка цены", "и колонка РРЦ"):
            await self.tools.execute("add_task", {
                "kind": "изменение цен", "tm": "Egger", "collection": "Vintage",
                "description": text})
        self.assertEqual(len(self.tools.collected), 1)
        self.assertIn("РРЦ", self.tools.collected[0].description)

    async def test_normalization_is_always_mark_level(self):
        """Нормализация не дробится по коллекциям, что бы ни передала модель.

        Имена собираются по единому шаблону §19.5 для всей марки, и код выполняет её
        одним проходом. Пять коллекций дали бы пять захватов, пять прогонов и пять строк
        в списке вместо одной.
        """
        from src.model.enums import TaskSubject
        await self.tools.execute("add_task", {
            "kind": "нормализация наименований", "tm": "Egger", "tm_code": "T1",
            "collection": "Vintage", "description": "привести к шаблону"})
        task = self.tools.collected[0]
        self.assertEqual(task.subject, TaskSubject.MARK)
        # подпись не «Egger / Egger», а просто марка
        self.assertEqual(task.address.label(), "Egger")

    async def test_normalization_of_two_collections_is_one_task(self):
        for collection in ("Vintage", "Adventure", "Retro"):
            await self.tools.execute("add_task", {
                "kind": "нормализация наименований", "tm": "Egger", "tm_code": "T1",
                "collection": collection, "description": f"по {collection}"})
        self.assertEqual(len(self.tools.collected), 1)

    async def test_prompt_forbids_judging_names_it_has_not_seen(self):
        """Выгрузки 1С у него нет — значит и судить о соответствии шаблону не по чему.

        Ровно это породило описание на десять строк про «единый ли шаблон» и «маркетинговые
        пометки»: агент рассуждал о том, чего не видел. Шаблон §19.5 живёт в коде, и
        сверяет с ним тоже код.
        """
        from src.model.task_builder import PROMPT
        self.assertIn("НЕ рассуждай о том, соответствуют ли наименования 1С шаблону",
                      PROMPT)
        self.assertIn("§19.5", PROMPT)

    async def test_normalization_needs_no_collection(self):
        out = await self.tools.execute("add_task", {
            "kind": "нормализация наименований", "tm": "Egger",
            "description": "привести к шаблону"})
        self.assertIn("заведена", out)
        self.assertEqual(len(self.tools.collected), 1)

    async def test_other_kinds_still_need_a_subject(self):
        out = await self.tools.execute("add_task", {
            "kind": "изменение цен", "tm": "Egger", "description": "…"})
        self.assertIn("не адресуема", out)

    async def test_unknown_kind_is_refused_with_the_list(self):
        out = await self.tools.execute("add_task", {
            "kind": "поправить всё", "tm": "Egger", "collection": "X", "description": "…"})
        self.assertIn("Неизвестный вид", out)
        self.assertIn("изменение цен", out)
        self.assertEqual(self.tools.collected, [])

    async def test_task_without_subject_is_refused(self):
        out = await self.tools.execute("add_task", {
            "kind": "изменение цен", "tm": "Egger", "description": "…"})
        self.assertIn("не адресуема", out)
        self.assertEqual(self.tools.collected, [])

    async def test_task_without_mark_is_refused(self):
        out = await self.tools.execute("add_task", {
            "kind": "изменение цен", "tm": "", "collection": "X", "description": "…"})
        self.assertIn("не адресуема", out)


class Folder:
    """Узел дерева папок 1С — столько полей, сколько читает `owner_map`."""

    def __init__(self, ref, name, parent_ref="", kind="collection"):
        self.ref = ref
        self.name = name
        self.parent_ref = parent_ref
        self.kind = kind


MARKS = [{"name": "Most Flooring", "code": "T311", "selling": True},
         {"name": "A+ Floor", "code": "T326", "selling": False}]

# «Ле Паркет» лежит под Most Flooring, «Миллениум Про» — под A+ Floor, хотя прайс у них
# общий. Ровно этот случай агент и путает.
TREE = [
    Folder("m1", "Most Flooring", kind="tm"),
    Folder("m2", "A+ Floor", kind="tm"),
    Folder("c1", "Ле Паркет", parent_ref="m1"),
    Folder("c2", "Миллениум Про", parent_ref="m2"),
    Folder("g1", "Ламинат", parent_ref="m2", kind="group"),
    Folder("c3", "Гранд", parent_ref="g1"),          # через промежуточную папку
]


class OwnerMapTest(unittest.TestCase):

    def setUp(self):
        from src.model.task_builder import owner_map
        self.owners = owner_map(TREE, MARKS)

    def test_collection_resolves_to_its_mark(self):
        self.assertEqual(self.owners["ле паркет"], {("T311", "Most Flooring")})
        self.assertEqual(self.owners["миллениум про"], {("T326", "A+ Floor")})

    def test_intermediate_folders_are_walked_through(self):
        """Между коллекцией и маркой бывает папка вида товара — идти надо вверх до ТМ."""
        self.assertEqual(self.owners["гранд"], {("T326", "A+ Floor")})

    def test_unknown_mark_is_skipped_not_guessed(self):
        tree = TREE + [Folder("m9", "Неизвестная", kind="tm"),
                       Folder("c9", "Сирота", parent_ref="m9")]
        from src.model.task_builder import owner_map
        self.assertNotIn("сирота", owner_map(tree, MARKS))

    def test_broken_tree_does_not_hang(self):
        """Папка, ссылающаяся на себя, не должна крутить цикл вечно."""
        from src.model.task_builder import owner_map
        loop = [Folder("x", "Петля", parent_ref="x")]
        self.assertEqual(owner_map(loop, MARKS), {})


class FixMarksTest(unittest.TestCase):

    def make(self, tm_code, tm_name, collection):
        from src.model.enums import TaskKind
        from src.model.refs import Ref, TaskAddress
        from src.model.task import PriceTask
        return PriceTask(kind=TaskKind.CHANGE_PRICES,
                         address=TaskAddress(tm=Ref.make(code=tm_code, names=[tm_name]),
                                             subject=Ref.make(names=[collection])),
                         description="—")

    def fix(self, tasks):
        from src.model.task_builder import fix_marks, owner_map
        return fix_marks(tasks, owner_map(TREE, MARKS))

    def test_wrong_mark_is_corrected(self):
        """Тот самый случай: коллекция A+ Floor приписана Most Flooring."""
        task = self.make("T311", "Most Flooring", "Миллениум Про")
        notes = self.fix([task])
        self.assertEqual(task.address.tm.code, "T326")
        self.assertIn("A+ Floor", task.address.tm.label())
        self.assertTrue(notes)

    def test_right_mark_is_left_alone_and_silent(self):
        task = self.make("T311", "Most Flooring", "Ле Паркет")
        self.assertEqual(self.fix([task]), [])
        self.assertEqual(task.address.tm.code, "T311")

    def test_unknown_collection_is_not_touched(self):
        """Коллекции нет в 1С — это штатный случай «её ещё не завели»."""
        task = self.make("T311", "Most Flooring", "Совсем Новая")
        self.assertEqual(self.fix([task]), [])
        self.assertEqual(task.address.tm.code, "T311")

    def test_ambiguous_collection_is_reported_not_moved(self):
        """Одноимённая коллекция у двух марок — переставлять наугад хуже, чем оставить."""
        tree = TREE + [Folder("c4", "Ле Паркет", parent_ref="m2")]
        from src.model.task_builder import fix_marks, owner_map
        task = self.make("T311", "Most Flooring", "Ле Паркет")
        notes = fix_marks([task], owner_map(tree, MARKS))
        self.assertEqual(task.address.tm.code, "T311")
        self.assertIn("нескольких марок", notes[0])

    def test_mark_level_tasks_are_skipped(self):
        """У задачи на марку целиком предмет — сама марка, сверять нечего."""
        from src.model.enums import TaskKind, TaskSubject
        from src.model.refs import Ref, TaskAddress
        from src.model.task import PriceTask
        mark = Ref.make(code="T311", names=["Most Flooring"])
        task = PriceTask(kind=TaskKind.NORMALIZE_NAMES,
                         address=TaskAddress(tm=mark, subject=mark,
                                             subject_kind=TaskSubject.MARK),
                         description="—")
        self.assertEqual(self.fix([task]), [])


class FakeOrchestrator:
    """Изображает агента: зовёт инструменты так, как это делала бы модель."""

    def __init__(self, calls):
        self._calls = calls
        self.systems = []

    async def handle_turn(self, history, system=None, extra_tools=None,
                          extra_executor=None, **kw):
        self.systems.append(system)
        for name, payload in self._calls:
            await extra_executor.execute(name, payload)
        return "готово", history


class BuildTest(unittest.IsolatedAsyncioTestCase):

    async def test_build_returns_what_the_agent_collected(self):
        orc = FakeOrchestrator([
            ("read_price", {}),
            ("get_selling_tm", {}),
            ("add_task", {"kind": "изменение цен", "tm": "Egger",
                          "collection": "Vintage", "description": "есть цены"}),
            ("add_task", {"kind": "добавление новых", "tm": "Egger",
                          "collection": "Adventure", "description": "проверить состав"}),
        ])
        tasks, answer = await build(orc, workbook(), "Прайс.xlsx", onec=FakeOnec())

        self.assertEqual(len(tasks), 2)
        self.assertEqual({t.address.subject.label() for t in tasks},
                         {"Vintage", "Adventure"})
        self.assertEqual(answer, "готово")

    async def test_prompt_forbids_inventing_unchecked_facts(self):
        """У агента нет выгрузки 1С, и промпт обязан это называть: иначе он напишет
        «завести 12 недостающих позиций», которых не считал."""
        orc = FakeOrchestrator([])
        await build(orc, workbook(), "Прайс.xlsx")
        prompt = orc.systems[0]
        self.assertIn("НЕ утверждай того, чего не проверял", prompt)
        self.assertIn("выгрузки номенклатуры 1С", prompt)


class ServiceFallbackTest(unittest.IsolatedAsyncioTestCase):
    """Пустой ответ агента не должен оставлять прайс без задач."""

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

    def make(self, build_tasks):
        return PriceListService(
            self.store, self.suppliers,
            save_file=lambda c, n: price_files.save(self.db, n, c),
            broadcaster=Broadcaster(), build_tasks=build_tasks)

    async def test_agent_tasks_are_used(self):
        async def builder(content, filename, price):
            from src.model.refs import Ref, TaskAddress
            from src.model.task import PriceTask
            return [PriceTask(kind=TaskKind.CHANGE_PRICES,
                              address=TaskAddress(tm=Ref.make(names=["Egger"]),
                                                  subject=Ref.make(names=["Vintage"])),
                              description="от агента")]

        model = self.make(builder)
        await model.load()
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")

        tasks = model.prices[0].tasks
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].description, "от агента")

    async def test_empty_answer_falls_back_to_the_stub(self):
        """Молчаливо пустой список админ прочтёт как «разбирать нечего»."""
        async def builder(content, filename, price):
            return []

        model = self.make(builder)
        await model.load()
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")

        tasks = model.prices[0].tasks
        self.assertTrue(tasks)
        self.assertIn("ЗАГЛУШКА", tasks[0].description)

    async def test_agent_failure_falls_back_too(self):
        async def builder(content, filename, price):
            raise RuntimeError("нет связи с моделью")

        model = self.make(builder)
        await model.load()
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")
        self.assertTrue(model.prices[0].tasks)


if __name__ == "__main__":
    unittest.main()
