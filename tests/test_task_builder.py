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


def price_workbook() -> bytes:
    """Лист с ценовыми колонками: шапка поставщика сверху, как в боевых прайсах."""
    import openpyxl
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "Ламинат"
    sheet.append(["Прайс ООО «Поставщик»", "", "", ""])
    sheet.append(["Артикул", "Наименование", "Дилерская, м²", "РРЦ, м²"])
    sheet.append(["3309", "Дуб Авила", "1 560,00", "2 370"])
    sheet.append(["3310", "Дуб Прато", 1560, 2370])
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
            not_exported=False, purchase=None, rrc=None):
        from src.onec.client import ItemProperty, NomItem
        return NomItem(
            ref=ref, id="", name=f"Ламинат Egger {collection} {site}", article=article,
            unit="м2", size="", product_type="Ламинат", collection=collection,
            parent=collection, collection_ref="F1", alt_units={},
            purchase=purchase, retail=None, rrc=rrc, site_name=site,
            product_type_ref="000000003", not_exported=not_exported,
            properties=tuple(ItemProperty(property=p, code="", value=v, value_code="")
                             for p, v in props))

    async def compare(self, tools, articles, collection="Vintage", columns=None):
        inp = {"tm_code": "T1", "collection": collection, "articles": articles}
        if columns:
            inp["price_columns"] = columns
        out = await tools.execute("compare_with_1c", inp)
        return json.loads(out)

    async def test_cyrillic_price_name_matches_latin_1c_name(self):
        """СЛУЧАЙ С БОЯ (21.09.2026). В 1С коллекции названы латиницей («Millenium Pro»),
        в прайсе кириллицей («Миллениум Про»). Поиск по имени не находил ничего, сверка
        отвечала «коллекции в 1С нет», и агент завёл СЕМЬ задач «завести всю коллекцию»
        по коллекциям, которые давно заведены: ~56 дублей, если бы их выполнили.

        Артикул одинаков в обоих источниках — по нему и сопоставляем.
        """
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx", onec=self.onec([
            self.nom("R1", "3309", collection="Millenium Pro"),
            self.nom("R2", "3310", collection="Millenium Pro"),
        ]))
        got = await self.compare(tools, ["3309", "3310"], collection="Миллениум Про")
        self.assertEqual(got["нашлось_в_1С"], 2)
        self.assertEqual(got["missing_in_1c"], [])
        # и агент узнаёт, как коллекция называется в справочнике
        self.assertEqual(got["collection_в_1С"], ["Millenium Pro"])

    async def test_prices_matching_the_file_report_nothing_to_do(self):
        """СЛУЧАЙ С БОЯ (21.09.2026). По Millenium Pro агент завёл задачу «обновить цены
        по 8 позициям», а закупка и РРЦ в 1С уже совпадали с прайсом: задача заводилась по
        факту наличия ценовых колонок. Теперь сравнение делает код, и агенту прямо
        сказано, что заводить нечего."""
        from src.onec.client import Price
        tools = TaskBuilderTools(price_workbook(), "Прайс.xlsx", onec=self.onec([
            self.nom("R1", "3309", purchase=Price(1560, None), rrc=Price(2370, None)),
            self.nom("R2", "3310", purchase=Price(1560, None), rrc=Price(2370, None)),
        ]))
        await tools.execute("read_price", {})
        got = await self.compare(tools, ["3309", "3310"], columns={
            "article": "Артикул", "purchase": "Дилерская", "rrc": "РРЦ"})

        self.assertEqual(got["цены"]["совпадают"], 2)
        self.assertEqual(got["цены"]["расходятся"], [])
        self.assertIn("НЕ заводи", got["цены"]["вывод"])

    async def test_price_difference_comes_back_with_numbers(self):
        from src.onec.client import Price
        tools = TaskBuilderTools(price_workbook(), "Прайс.xlsx", onec=self.onec([
            self.nom("R1", "3309", purchase=Price(1200, None), rrc=Price(2370, None)),
        ]))
        await tools.execute("read_price", {})
        got = await self.compare(tools, ["3309"], columns={
            "article": "Артикул", "purchase": "Дилерская", "rrc": "РРЦ"})

        self.assertEqual(len(got["цены"]["расходятся"]), 1)
        self.assertIn("1 200", got["цены"]["расходятся"][0])

    async def test_columns_are_named_once_per_sheet(self):
        """Повторять их на каждой коллекции — лишние выходные токены на ровном месте."""
        from src.onec.client import Price
        tools = TaskBuilderTools(price_workbook(), "Прайс.xlsx", onec=self.onec([
            self.nom("R1", "3309", purchase=Price(1560, None), rrc=Price(2370, None)),
        ]))
        await tools.execute("read_price", {})
        await self.compare(tools, ["3309"], columns={
            "article": "Артикул", "purchase": "Дилерская", "rrc": "РРЦ"})

        again = await self.compare(tools, ["3309"])
        self.assertEqual(again["цены"]["совпадают"], 1)

    async def test_without_columns_the_answer_says_so(self):
        """Молчаливое «совпадают: 0» агент прочёл бы как «менять нечего»."""
        tools = TaskBuilderTools(price_workbook(), "Прайс.xlsx",
                                 onec=self.onec([self.nom("R1", "3309")]))
        got = await self.compare(tools, ["3309"])
        self.assertIn("колонки_не_названы", got["цены"])

    async def test_collection_under_another_mark_is_found(self):
        """СЛУЧАЙ С БОЯ (22.09.2026). «Классик» лежит под маркой A+ Floor, а модель сверяла
        её с Most Flooring — как написано на обложке прайса. Под той маркой артикулов нет,
        сверка отвечала «коллекция новая», сверять цены становилось не с чем, и задача не
        заводилась ВООБЩЕ. Принадлежность знает 1С, а не догадка модели."""
        from src.onec.client import FoundItem, FoundItems, Price

        mine = self.onec([self.nom("R1", "3309", collection="Millenium Pro")])

        class Fake(mine.__class__):
            def by_tm_all(self, tm_code, **kw):
                if tm_code == "T9":     # марка, под которой лежит «Классик»
                    return type(mine.by_tm_all("T1"))([
                        CompareTest.nom(self, "R9", "301", collection="Классик",
                                        purchase=None, rrc=None)])
                return mine.by_tm_all(tm_code, **kw)

            def find_items(self, articles=None, limit=50, **kw):
                return FoundItems(items=[FoundItem(
                    ref="YO-9", name="Ламинат A+ Floor Классик Аристо", article="301",
                    tm="A+ Floor", tm_code="T9", parent_name="Классик 600x238x12")],
                    total=1)

        tools = TaskBuilderTools(price_workbook(), "Прайс.xlsx", onec=Fake())
        got = await self.compare(tools, ["301"], collection="Классик")

        self.assertEqual(got["марка_в_1С"]["tm_code"], "T9")
        self.assertEqual(got["нашлось_в_1С"], 1)
        self.assertEqual(got["цены"]["без_цены_в_1С"], 1)

    async def test_missing_price_in_1c_is_seen_without_the_file(self):
        """СЛУЧАЙ С БОЯ (22.09.2026): у «Классик» (A+ Floor) не было ни закупки, ни РРЦ ни
        у одной из восьми позиций. Ответ «колонки цен не названы» звучал как «сверить
        нечем», и задача не заводилась — хотя для такого вывода прайс не нужен вовсе."""
        tools = TaskBuilderTools(price_workbook(), "Прайс.xlsx",
                                 onec=self.onec([self.nom("R1", "3309")]))
        got = await self.compare(tools, ["3309"])
        self.assertEqual(got["цены"]["без_цены_в_1С"], 1)
        self.assertIn("задачу заводи", got["цены"]["вывод"])

    async def test_articles_lying_in_discontinued_are_a_revival(self):
        """Коллекцию однажды унесли в снятые, а поставщик привёз её снова. Выгрузка марки
        её не видит, и без поиска по всей базе агент завёл бы всё заново — сто дублей с
        потерянной историей цен."""
        from src.onec.client import FoundItem, FoundItems

        class Fake(self.onec([self.nom("R1", "3309")]).__class__):
            def find_items(self, articles=None, limit=50, **kw):
                return FoundItems(items=[FoundItem(
                    ref="YO-00006107", name="Дуб Прато", article="3310",
                    parent_name="Снятые с производства Ламинат",
                    not_exported=True)], total=1)

        tools = TaskBuilderTools(workbook(), "Прайс.xlsx", onec=Fake())
        got = await self.compare(tools, ["3309", "3310"], collection="Millenium Pro")
        self.assertIn("YO-00006107", got["уже_есть_в_1С_вне_коллекции"][0])

        await tools.execute("add_task", {
            "kind": "добавление новых", "tm": "Egger", "tm_code": "T1",
            "collection": "Millenium Pro", "description": "завести 3310"})
        text = tools.collected[0].description
        # текст пишет КОД: коды 1С — то, по чему задача исполняется
        self.assertIn("ВОЗВРАТ РАНЕЕ СНЯТОГО", text)
        self.assertIn("YO-00006107", text)
        self.assertIn("Снятые с производства Ламинат", text)

    async def test_revival_note_only_lands_on_add_new(self):
        """У задачи по ценам или свойствам возврата из снятых быть не может."""
        from src.onec.client import FoundItem, FoundItems

        class Fake(self.onec([self.nom("R1", "3309")]).__class__):
            def find_items(self, articles=None, limit=50, **kw):
                return FoundItems(items=[FoundItem(ref="YO-1", name="Х", article="3310")],
                                  total=1)

        tools = TaskBuilderTools(workbook(), "Прайс.xlsx", onec=Fake())
        await self.compare(tools, ["3309", "3310"], collection="Millenium Pro")
        await tools.execute("add_task", {
            "kind": "изменение цен", "tm": "Egger", "tm_code": "T1",
            "collection": "Millenium Pro", "description": "цены"})
        self.assertNotIn("ВОЗВРАТ", tools.collected[0].description)

    async def test_truly_new_collection_says_so_with_a_caveat(self):
        """Ни один артикул не нашёлся — либо коллекция правда новая, либо колонка
        артикула прочитана не та. Второе стоит проверить до заведения всего заново."""
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx",
                                 onec=self.onec([self.nom("R1", "3309")]))
        got = await self.compare(tools, ["9001", "9002"], collection="Совсем Новая")
        self.assertEqual(got["нашлось_в_1С"], 0)
        self.assertIn("колонку артикула", got["note"])

    async def test_untouched_collections_are_found(self):
        """Коллекция, которой в прайсе НЕТ, в обход по прайсу не попадает никогда — её
        не о чем спрашивать. Так осталась незамеченной Brilliant у Most Flooring."""
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx", onec=self.onec([
            self.nom("R1", "3309", collection="Millenium Pro"),
            self.nom("R2", "A11701", collection="Brilliant"),
        ]))
        await self.compare(tools, ["3309"], collection="Миллениум Про")
        self.assertEqual(tools.untouched_collections(), {"T1": ["Brilliant"]})

    async def test_discontinued_collections_are_not_candidates_again(self):
        """Снятую коллекцию звать снимать заново незачем."""
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx", onec=self.onec([
            self.nom("R1", "3309", collection="Millenium Pro"),
            self.nom("R2", "X1", collection="Prestige", not_exported=True),
        ]))
        await self.compare(tools, ["3309"], collection="Миллениум Про")
        self.assertEqual(tools.untouched_collections(), {})

    async def test_collection_of_another_supplier_is_not_discontinued(self):
        """СЛУЧАЙ, РАДИ КОТОРОГО ЗАВЕДЁН ЖУРНАЛ. Коллекции нет в этом прайсе, но её возит
        другой поставщик: это смена канала, а не снятие с производства. Задачи быть не
        должно — вместо неё строка админу."""
        from src.model.task_builder import _add_discontinued_candidates
        from src.storage.sightings import Sighting

        tools = TaskBuilderTools(workbook(), "Прайс.xlsx", onec=self.onec([
            self.nom("R1", "3309", collection="Millenium Pro"),
            self.nom("R2", "A11701", collection="Brilliant"),
        ]), elsewhere={"a11701": Sighting(supplier_id=9, supplier="Паркет-Холл",
                                          collection="Бриллиант",
                                          price_date="2026-09-15")})
        await self.compare(tools, ["3309"], collection="Миллениум Про")
        await tools.execute("add_task", {"kind": "изменение цен", "tm": "Egger",
                                         "tm_code": "T1", "collection": "Millenium Pro",
                                         "description": "цены"})

        notes = _add_discontinued_candidates(tools)
        kinds = [t.kind for t in tools.collected]
        self.assertNotIn(TaskKind.MOVE_DISCONTINUED, kinds)
        self.assertIn("Паркет-Холл", notes[0])

    async def test_unknown_collection_is_still_discontinued(self):
        """Пока журнал пуст, поведение прежнее: обратного никто не показывал."""
        from src.model.task_builder import _add_discontinued_candidates

        tools = TaskBuilderTools(workbook(), "Прайс.xlsx", onec=self.onec([
            self.nom("R1", "3309", collection="Millenium Pro"),
            self.nom("R2", "A11701", collection="Brilliant"),
        ]))
        await self.compare(tools, ["3309"], collection="Миллениум Про")
        await tools.execute("add_task", {"kind": "изменение цен", "tm": "Egger",
                                         "tm_code": "T1", "collection": "Millenium Pro",
                                         "description": "цены"})

        self.assertEqual(_add_discontinued_candidates(tools), [])
        self.assertIn(TaskKind.MOVE_DISCONTINUED,
                      [t.kind for t in tools.collected])

    async def test_gone_item_carried_by_another_is_told_apart(self):
        """В сверке по коллекции — то же самое, но по одной позиции."""
        from src.storage.sightings import Sighting
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx", onec=self.onec([
            self.nom("R1", "3309"), self.nom("R2", "3310"), self.nom("R3", "3311"),
        ]), elsewhere={"3311": Sighting(9, "Паркет-Холл", "Про", "2026-09-15")})

        got = await self.compare(tools, ["3309"])
        self.assertEqual(got["missing_in_price"], ["3310 Бах"])
        self.assertIn("Паркет-Холл", got["есть_у_другого_поставщика"][0])

    async def test_articles_of_this_price_go_into_the_journal(self):
        """Журнал наполняется тем, что модель и так перечислила: ноль лишних токенов."""
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx",
                                 onec=self.onec([self.nom("R1", "3309")]))
        await self.compare(tools, ["3309", "LE-263"], collection="Миллениум Про")
        self.assertEqual(tools.seen_articles,
                         {"3309": "Миллениум Про", "le263": "Миллениум Про"})

    async def test_missing_in_1c_is_named_not_counted(self):
        """Админу нужны артикулы, а не «трёх не хватает»: по числу работать нельзя."""
        tools = TaskBuilderTools(workbook(), "Прайс.xlsx",
                                 onec=self.onec([self.nom("R1", "3309")]))
        got = await self.compare(tools, ["3309", "3311 Бетховен", "3315"])
        self.assertEqual(got["missing_in_1c"], ["3311 Бетховен", "3315"])
        self.assertEqual(got["нашлось_в_1С"], 1)

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
        self.assertEqual(got["нашлось_в_1С"], 0)
        self.assertIsNone(got["collection_в_1С"])

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
                              description="от агента")], "завёл одну задачу"

        model = self.make(builder)
        await model.load()
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")

        tasks = model.prices[0].tasks
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].description, "от агента")

    async def test_nothing_to_do_is_a_result_not_a_stub(self):
        """СЛУЧАЙ С БОЯ (21.09.2026). По разобранному прайсу агент честно не нашёл работы
        — цены совпали, нормализация отклонена как пустая, — а код подставил пять
        заглушек, каждая из которых при выполнении ничего бы не сделала."""
        said = []

        async def builder(content, filename, price):
            return [], "Расхождений нет: цены совпадают, позиции заведены."

        class Ear:
            async def notify(self, event):
                said.append(event.text)

        model = self.make(builder)
        model.events.subscribe(Ear())
        await model.load()
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")

        self.assertEqual(model.prices[0].tasks, [])
        told = " ".join(said)
        self.assertIn("работы нет", told)
        # и слова агента доезжают: без них «работы нет» неотличимо от «прочитал не тот лист»
        self.assertIn("цены совпадают", told)

    async def test_agent_failure_falls_back_to_the_stub(self):
        """Заглушка остаётся ровно для сорвавшегося прогона: тут пустота была бы ложью."""
        said = []

        class Ear:
            async def notify(self, event):
                said.append(event.text)

        async def builder(content, filename, price):
            raise RuntimeError("credit balance is too low")

        model = self.make(builder)
        model.events.subscribe(Ear())
        await model.load()
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")
        tasks = model.prices[0].tasks
        self.assertTrue(tasks)
        self.assertIn("ЗАГЛУШКА", tasks[0].description)
        # причина обязана доехать до админа: в консоль из Telegram не заглянешь
        self.assertIn("credit balance", " ".join(said))

    async def test_broken_rebuild_keeps_the_tasks_it_had(self):
        """СЛУЧАЙ С БОЯ (21.09.2026): прогон упал посреди хода на 400 «credit balance is
        too low». На новом прайсе это стоило пяти заглушек, а на пересборке стоило бы
        всего живого списка — задачи не устарели от того, что модель не ответила."""
        from src.model.commands import Command, CommandKind
        from src.model.refs import Ref, TaskAddress
        from src.model.task import PriceTask

        good = [PriceTask(kind=TaskKind.CHANGE_PRICES,
                          address=TaskAddress(tm=Ref.make(names=["Egger"]),
                                              subject=Ref.make(names=["Vintage"])),
                          description="настоящая задача")]
        answers = [(good, "собрал")]

        async def builder(content, filename, price):
            if answers:
                return answers.pop()
            raise RuntimeError("credit balance is too low")

        model = self.make(builder)
        await model.load()
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")
        price_id = model.prices[0].id

        await model.apply(Command(kind=CommandKind.REBUILD_TASKS, price_id=price_id,
                                  actor="admin"))

        tasks = model.prices[0].tasks
        self.assertEqual([t.description for t in tasks], ["настоящая задача"])


if __name__ == "__main__":
    unittest.main()
