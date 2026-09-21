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

    async def test_normalization_description_is_written_by_code(self):
        """Стандарт формулирует код, а не модель.

        Агент дважды выдумывал шаблон и оба раза неверно — последний раз «марка +
        коллекция + декор + артикул», хотя артикул в наименованиях не используется вообще,
        а вид товара стоит первым. Записать выдумку в 1С он не может (нормализацию делает
        код и описание не читает), но описание читает АДМИН.
        """
        await self.tools.execute("add_task", {
            "kind": "нормализация наименований", "tm": "Egger", "tm_code": "T1",
            "description": "шаблон: марка + коллекция + декор + артикул"})
        text = self.tools.collected[0].description
        self.assertIn("§19.5", text)
        self.assertIn("Артикул в наименованиях НЕ используется", text)

    async def test_agent_observation_survives_but_is_labelled(self):
        """В прайсе он правда кое-что видит — терять это не надо, но и выдавать за
        стандарт 1С нельзя."""
        await self.tools.execute("add_task", {
            "kind": "нормализация наименований", "tm": "Egger", "tm_code": "T1",
            "description": "в заголовках прайса пометки NEW и АКЦИЯ"})
        text = self.tools.collected[0].description
        self.assertIn("Замечено в прайсе", text)
        self.assertIn("NEW", text)
        # стандарт стоит ВЫШЕ наблюдения: его читают первым
        self.assertLess(text.index("§19.5"), text.index("Замечено в прайсе"))

    def test_sample_comes_from_build_name_and_cannot_drift(self):
        """Пример собирается той же функцией, что и настоящие имена: поменяется шаблон —
        поменяется и описание, вручную синхронизировать нечего."""
        from src.price_tool.items import build_name
        from src.model.task_builder import normalize_brief
        self.assertIn(build_name("Ламинат", "Egger", "Vintage", "Дуб Медовый"),
                      normalize_brief())

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


class PointlessNormalizationTest(unittest.IsolatedAsyncioTestCase):
    """Задачу, которой нечего делать, не заводят.

    СЛУЧАЙ С БОЯ (21.09.2026). У Most Flooring восемь коллекций, сто позиций — и ни одной
    правки: имена давно собраны по шаблону. Админ открыл задачу, выполнил и увидел
    «менять было нечего». Нормализация адресуется марке целиком и до сих пор не проходила
    никакой проверки, в отличие от остальных видов, где есть `compare_with_1c`.
    """

    def onec(self, names):
        from src.onec.client import NomItem

        # `full_name` заполняется тем же, что и `name`: в 1С он так и стоит, а пустой
        # дал бы правку сам по себе — и «уже канонический» образец перестал бы им быть.
        items = [NomItem(ref=f"R{n}", id="", name=name, article=f"A{n}", unit="м2",
                         size="", product_type="Ламинат", collection="Brilliant",
                         parent="Brilliant", collection_ref="F1", alt_units={},
                         purchase=None, retail=None, rrc=None, site_name=site,
                         full_name=name, product_type_ref="000000003")
                 for n, (name, site) in enumerate(names)]

        class Nom:
            def __init__(self):
                self.items = items
                self.tm = "Egger"
                self.total = len(items)
                self.errors = []

        class Fake(FakeOnec):
            def by_tm_all(self, tm_code, **kw):
                return Nom()

        return Fake()

    async def add(self, tools):
        return await tools.execute("add_task", {
            "kind": "нормализация наименований", "tm": "Egger", "tm_code": "T1",
            "description": "привести к шаблону"})

    async def test_canonical_names_produce_no_task(self):
        onec = self.onec([("Ламинат Egger Brilliant Дуб серый", "Дуб серый")])
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx", onec=onec)
        out = await self.add(tools)
        self.assertEqual(tools.collected, [])
        self.assertIn("не нужна", out)

    async def test_broken_names_do_produce_a_task(self):
        onec = self.onec([("Egger Brilliant Дуб серый", "Дуб серый")])  # нет вида товара
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx", onec=onec)
        await self.add(tools)
        self.assertEqual(len(tools.collected), 1)

    async def test_a_failed_check_still_creates_the_task(self):
        """Не сумев проверить, безопаснее завести: админ увидит «менять было нечего», а
        вот пропущенная работа не всплывёт никак."""
        class Broken(FakeOnec):
            def by_tm_all(self, tm_code, **kw):
                raise RuntimeError("1С недоступна")

        tools = TaskBuilderTools(workbook(), "Прайс.xlsx", onec=Broken())
        await self.add(tools)
        self.assertEqual(len(tools.collected), 1)

    async def test_without_1c_the_task_is_created(self):
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx")
        await self.add(tools)
        self.assertEqual(len(tools.collected), 1)


class PriceNotesTest(unittest.TestCase):
    """Повтор стандарта вычищается из наблюдения агента.

    Ему велено «не описывай шаблон», но он всё равно начинает с «Привести наименования к
    шаблону §19.5» — и описание выходит с двумя одинаковыми предложениями подряд. Это не
    в укор: он пишет описание задачи, а то, что стандарт допишет код, ему невидимо.
    """

    def notes(self, text):
        from src.model.task_builder import _price_notes_only
        return _price_notes_only(text)

    def test_the_standard_is_stripped_but_observations_stay(self):
        out = self.notes("Привести наименования марки к шаблону §19.5. "
                         "В заголовках пометки NEW и АКЦИЯ, их тащить не нужно.")
        self.assertNotIn("19.5", out)
        self.assertIn("NEW", out)

    def test_nothing_but_the_standard_leaves_nothing(self):
        self.assertEqual(self.notes("Привести наименования марки к шаблону §19.5."), "")

    def test_pure_observations_pass_untouched(self):
        text = "Бренд в файле пишется как MOST FLOOR, в 1С марка «Most Flooring»."
        self.assertEqual(self.notes(text), text)

    def test_empty_stays_empty(self):
        self.assertEqual(self.notes(None), "")
        self.assertEqual(self.notes("   "), "")


class CompareTest(unittest.IsolatedAsyncioTestCase):
    """Сверка коллекции с 1С — то, ради чего задачи стали конкретными.

    Без неё агент мог написать только «проверить, все ли 8 декоров заведены». После
    сверки он знает: трёх нет, и пишет именно это.
    """

    def onec(self, items, tm="Egger"):
        from src.onec.client import NomItem

        class Nom:
            def __init__(self, xs):
                self.items = list(xs)
                self.tm = tm
                self.total = len(self.items)
                self.errors = []

        class Fake(FakeOnec):
            def by_tm_all(self, tm_code, **kw):
                return Nom(items)

        return Fake()

    def nom(self, ref, article, site="Бах", collection="Vintage", props=(),
            not_exported=False):
        from src.onec.client import ItemProperty, NomItem
        return NomItem(
            ref=ref, id="", name=f"Ламинат Egger {collection} {site}", article=article,
            unit="м2", size="", product_type="Ламинат", collection=collection,
            parent=collection, collection_ref="F1", alt_units={},
            purchase=None, retail=None, rrc=None, site_name=site,
            product_type_ref="000000003", not_exported=not_exported,
            properties=tuple(ItemProperty(property=p, code="", value=v, value_code="")
                             for p, v in props))

    async def compare(self, tools, articles, collection="Vintage"):
        out = await tools.execute("compare_with_1c", {
            "tm_code": "T1", "collection": collection, "articles": articles})
        return json.loads(out)

    async def test_missing_in_1c_is_named_not_counted(self):
        """Админу нужны артикулы, а не «трёх не хватает»: по числу работать нельзя."""
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx",
                                 onec=self.onec([self.nom("R1", "3309")]))
        got = await self.compare(tools, ["3309", "3311 Бетховен", "3315"])
        self.assertEqual(got["missing_in_1c"], ["3311 Бетховен", "3315"])
        self.assertEqual(got["in_1c"], 1)

    async def test_articles_match_regardless_of_separators(self):
        """«LE-263», «LE 263» и «le263» — один артикул: поставщики пишут как придётся."""
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx",
                                 onec=self.onec([self.nom("R1", "LE-263")]))
        got = await self.compare(tools, ["le 263"])
        self.assertEqual(got["missing_in_1c"], [])

    async def test_missing_in_price_is_a_candidate_not_a_verdict(self):
        """Поставщик мог прислать частичный прайс — решать админу."""
        tools = TaskBuilderTools(
            workbook(), "Прайс.xlsx",
            onec=self.onec([self.nom("R1", "3309"), self.nom("R2", "3301", site="Лист")]))
        got = await self.compare(tools, ["3309"])
        self.assertEqual(len(got["missing_in_price"]), 1)
        self.assertIn("3301", got["missing_in_price"][0])

    async def test_discontinued_are_not_counted_as_missing_from_the_price(self):
        """Снятые в прайсе и не должны быть — иначе каждая сверка звала бы снимать их
        заново."""
        tools = TaskBuilderTools(
            workbook(), "Прайс.xlsx",
            onec=self.onec([self.nom("R1", "3309"),
                            self.nom("R2", "3301", not_exported=True)]))
        got = await self.compare(tools, ["3309"])
        self.assertEqual(got["missing_in_price"], [])

    async def test_empty_properties_are_those_empty_everywhere(self):
        """«Ни у одной», а не «у какой-нибудь»: разнобой внутри коллекции — другой
        разговор, а сплошь пустое свойство просто не заводили."""
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx", onec=self.onec([
            self.nom("R1", "3309", props=[("Класс", ""), ("Фаска", "V4")]),
            self.nom("R2", "3310", props=[("Класс", ""), ("Фаска", "")]),
        ]))
        got = await self.compare(tools, ["3309", "3310"])
        self.assertEqual(got["empty_properties"], ["Класс"])

    async def test_absent_collection_is_named_as_such(self):
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx",
                                 onec=self.onec([self.nom("R1", "3309")]))
        got = await self.compare(tools, ["9001"], collection="Совсем Новая")
        self.assertEqual(got["in_1c"], 0)
        self.assertIn("нет вовсе", got["note"])

    async def test_nomenclature_does_not_leak_into_the_answer(self):
        """В ЭТОМ ВСЯ ЭКОНОМИЯ: выгрузка поехала бы в каждый следующий запрос."""
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx",
                                 onec=self.onec([self.nom("R1", "3309")]))
        out = await tools.execute("compare_with_1c", {
            "tm_code": "T1", "collection": "Vintage", "articles": ["3309"]})
        self.assertNotIn("Ламинат Egger Vintage", out)
        self.assertLess(len(out), 400)

    async def test_without_1c_it_says_so_instead_of_guessing(self):
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx")
        out = await tools.execute("compare_with_1c", {
            "tm_code": "T1", "collection": "Vintage", "articles": ["3309"]})
        self.assertIn("не настроена", out)


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
        # Раньше правило звучало «выгрузки у тебя нет». Теперь сверка есть, и правило
        # стало сильнее: не «пиши проверить», а «сначала спроси, потом пиши числа».
        self.assertIn("сначала спроси", prompt)
        self.assertIn("ПИШИ ТО, ЧТО ЗНАЕШЬ, А НЕ ТО, ЧТО НАДО ПРОВЕРИТЬ", prompt)


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
