"""Нормализация наименований кодом, без обращения к модели (§6.2).

ЧТО ЭТО ПРОВЕРЯЕТ. Не то, как собирается имя — за это отвечает `price_tool.items` и его
собственные тесты, — а РЕШЕНИЯ вокруг: какие позиции берём в работу, какие выносим админу,
откуда берётся имя марки и что попадает в отчёт.

Главное свойство этого пути — детерминированность: модель не участвует, значит один и тот
же вход обязан давать один и тот же выход, и проверять это можно без сети.
"""
import unittest
from decimal import Decimal

from src.model import normalize as nz
from src.model.enums import TaskKind, TaskStatus, TaskSubject
from src.model.executor import run_discontinue, run_normalization
from src.model.refs import Ref, TaskAddress
from src.model.task import PriceTask
from src.onec.client import NomItem


#: Коды видов товара в 1С (`ВидТовара.Код`) — по ним `naming.size_in_name` решает, пишется
#: ли размер в наименование. У керамики пишется, у ламината нет.
CERAMIC_CODE = "000000010"
LAMINATE_CODE = "000000005"


def item(ref="T1", article="3309", site="Бах", collection="Миллениум Про",
         product_type="Ламинат", name="", size="", not_exported=False,
         product_type_ref=LAMINATE_CODE):
    return NomItem(
        ref=ref, id="", name=name or f"Ламинат MOST FLOOR {collection} {site}",
        article=article, unit="м2", size=size, product_type=product_type,
        collection=collection, parent=collection, collection_ref="F1", alt_units={},
        purchase=None, retail=None, rrc=None,
        full_name="", site_name=site, product_type_ref=product_type_ref,
        not_exported=not_exported)


class FakeNom:
    def __init__(self, items, tm="Most Flooring"):
        self.items = list(items)
        self.tm = tm
        self.total = len(self.items)
        self.errors = []


class FakeOnec:
    def __init__(self, items, tm="Most Flooring"):
        self._items = list(items)
        self._tm = tm
        self.writes = []

    def by_tm_all(self, tm_code, **kw):
        return FakeNom(self._items, tm=self._tm)

    def set_items(self, ops):
        self.writes.append(ops)
        return {"created": 0, "updated": len(ops), "errors": []}


def task(collection="Миллениум Про", tm_code="000000311"):
    """Задача нормализации по ОДНОЙ коллекции.

    Такие теперь не создаются — `add_task` делает их на марку целиком, — но в базе лежат
    заведённые прежней версией, и обрабатывать их надо.
    """
    return PriceTask(
        kind=TaskKind.NORMALIZE_NAMES,
        address=TaskAddress(tm=Ref.make(code=tm_code, names=["Most Flooring"]),
                            subject=Ref.make(names=[collection]),
                            subject_kind=TaskSubject.COLLECTION),
        description="привести к шаблону", id=5)


def mark_task(tm_code="000000311"):
    """Задача на МАРКУ ЦЕЛИКОМ — то, что заводит `add_task` сейчас."""
    mark = Ref.make(code=tm_code, names=["Most Flooring"])
    return PriceTask(kind=TaskKind.NORMALIZE_NAMES,
                     address=TaskAddress(tm=mark, subject=mark,
                                         subject_kind=TaskSubject.MARK),
                     description="привести к шаблону", id=5)


def allow():
    return None


class SharedTailTest(unittest.TestCase):
    """Повторяющийся хвост в названиях расцветок — шум (случай Westerhof, 24.09.2026).

    Поставщик делает коллекции на двух заводах и дописал это в каждое наименование:
    «Альфа PELI», «Альпы AGT». Завод — свойство коллекции, а не расцветки.
    """

    def test_factory_marker_is_noise(self):
        titles = ["Альфа PELI", "Вега PELI", "Гамма PELI", "Ореон PELI"]
        noise = nz.shared_tail(titles)
        self.assertEqual(noise, {"peli"})
        self.assertEqual([nz.drop_shared(t, noise) for t in titles],
                         ["Альфа", "Вега", "Гамма", "Ореон"])

    def test_marker_on_half_the_items_still_counts(self):
        """У «Effect» пометка AGT стоит у семи позиций из четырнадцати: поставщик
        проставил её не везде, и требование «у всех» обессмыслило бы правило."""
        titles = ["Альпы AGT", "Тибет AGT", "Логан", "Соларо", "Фудзияма AGT"]
        noise = nz.shared_tail(titles)
        self.assertEqual(noise, {"agt"})
        self.assertEqual(nz.drop_shared("Логан", noise), "Логан")
        self.assertEqual(nz.drop_shared("Альпы AGT", noise), "Альпы")

    def test_genus_word_in_front_is_never_touched(self):
        """«Дуб» повторяется у всех, но он ЧАСТЬ названия. Разделяет их положение:
        род стоит спереди, маркер сзади."""
        titles = ["Дуб Авила", "Дуб Прато", "Дуб Ява"]
        self.assertEqual(nz.shared_tail(titles), set())

    def test_unique_titles_give_no_noise(self):
        self.assertEqual(nz.shared_tail(["Капри", "Наполи", "Гарда"]), set())

    def test_single_word_titles_are_left_alone(self):
        """Из «Капри» снимать нечего: имя из одного слова — это и есть расцветка."""
        self.assertEqual(nz.shared_tail(["Капри", "Капри"]), set())

    def test_noise_in_the_middle_survives(self):
        """Слово внутри имени означает, что строку мы поняли неверно; молча кромсать
        середину опаснее, чем оставить как есть."""
        self.assertEqual(nz.drop_shared("Дуб PELI Медовый", {"peli"}),
                         "Дуб PELI Медовый")

    def test_title_never_becomes_empty(self):
        self.assertEqual(nz.drop_shared("PELI", {"peli"}), "PELI")

    def test_several_markers_are_stripped_at_once(self):
        titles = ["Альфа PELI NEW", "Вега PELI NEW", "Гамма PELI NEW"]
        noise = nz.shared_tail(titles)
        self.assertEqual(nz.drop_shared("Альфа PELI NEW", noise), "Альфа")

    def test_plan_cleans_titles_of_the_whole_collection(self):
        """Шум считается по коллекции целиком: одна позиция о повторе не знает ничего."""
        items = [item(ref="T1", article="CO512", site="Альфа PELI", collection="Cosmo"),
                 item(ref="T2", article="CO520", site="Вега PELI", collection="Cosmo")]
        inputs, _skipped, _dropped, _noise = nz.plan(items, "000000005", "Westerhof")
        self.assertEqual([i["title"] for i in inputs[0]["items"]], ["Альфа", "Вега"])


class PlanTest(unittest.TestCase):
    """Что берём в работу, а что выносим админу."""

    def test_normal_items_are_planned(self):
        inputs, skipped, dropped, _noise = nz.plan([item(), item(ref="T2", article="1001",
                                                        site="Дуб Ява")],
                                           "000000311", "Most Flooring")
        self.assertEqual(len(inputs), 1)
        self.assertEqual(len(inputs[0]["items"]), 2)
        self.assertEqual(skipped, [])

    def test_title_comes_from_site_name(self):
        """`site_name` по §19.5 — это РОВНО название расцветки, готовый `title`."""
        inputs, _, _, _ = nz.plan([item(site="Дуб Ява")], "000000311", "Most Flooring")
        self.assertEqual(inputs[0]["items"][0]["title"], "Дуб Ява")

    def test_brand_is_the_1c_one(self):
        inputs, _, _, _ = nz.plan([item()], "000000311", "Most Flooring")
        self.assertEqual(inputs[0]["tm_name"], "Most Flooring")

    def test_item_without_a_title_goes_to_the_admin(self):
        """Название расцветки выводить неоткуда — выдумывать код не будет."""
        inputs, skipped, _, _ = nz.plan([item(site="")], "000000311", "Most Flooring")
        self.assertEqual(inputs, [])
        self.assertEqual(skipped[0][1], nz.Skipped.NO_TITLE)

    def test_contaminated_title_goes_to_the_admin(self):
        """Марка внутри названия расцветки попала бы в имя дважды."""
        inputs, skipped, _, _ = nz.plan([item(site="Most Flooring Бах")],
                                     "000000311", "Most Flooring")
        self.assertEqual(inputs, [])
        self.assertEqual(skipped[0][1], nz.Skipped.DIRTY_TITLE)

    def test_collection_inside_the_title_is_caught_too(self):
        inputs, skipped, _, _ = nz.plan([item(site="Миллениум Про Бах")],
                                     "000000311", "Most Flooring")
        self.assertEqual(skipped[0][1], nz.Skipped.DIRTY_TITLE)

    def test_short_words_do_not_block_normalization(self):
        """«Про» или «Ле» встречаются внутри расцветок как обычные слова: запрет по ним
        выключил бы нормализацию целым коллекциям."""
        inputs, skipped, _, _ = nz.plan([item(site="Про Бах", collection="Ле")],
                                     "000000311", "Most Flooring")
        self.assertEqual(skipped, [])
        self.assertEqual(len(inputs), 1)

    def test_discontinued_are_left_alone(self):
        """Снятые лежат в невыгружаемых папках, на сайт не идут — переименование им
        ничего не даёт, а пачку записи раздувает."""
        inputs, skipped, dropped, _noise = nz.plan(
            [item(), item(ref="T2", not_exported=True)], "000000311", "Most Flooring")
        self.assertEqual(dropped, 1)
        self.assertEqual(len(inputs[0]["items"]), 1)
        self.assertEqual(skipped, [])

    def test_only_collection_narrows_the_work(self):
        inputs, _, _, _ = nz.plan(
            [item(collection="Миллениум Про"), item(ref="T2", collection="Ле Паркет")],
            "000000311", "Most Flooring", only_collection="Ле Паркет")
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["collection"], "Ле Паркет")

    def test_groups_split_by_collection_and_type(self):
        """`plan_collection` работает на одну коллекцию и вид — бить обязаны мы."""
        inputs, _, _, _ = nz.plan(
            [item(collection="A"), item(ref="T2", collection="B"),
             item(ref="T3", collection="A", product_type="Плитка")],
            "000000311", "Most Flooring")
        self.assertEqual(len(inputs), 3)


class ProductTypeTest(unittest.IsolatedAsyncioTestCase):
    """Под одной маркой лежат РАЗНЫЕ виды товара, и шаблон имени у каждого свой.

    Задача одна на марку — значит развилку по видам обязан держать код. Проверяется не то,
    что групп получилось много (это `PlanTest`), а что к каждой применился СВОЙ шаблон.
    """

    def mixed(self):
        """Керамика и ламинат у одной марки, у обоих размер заполнен."""
        return [
            item(ref="C1", collection="Мадейра", product_type="Керамическая плитка",
                 product_type_ref=CERAMIC_CODE, site="Беж", size="60x60",
                 name="MOST FLOOR Мадейра Беж"),
            item(ref="L1", collection="Ле Паркет", product_type="Ламинат",
                 product_type_ref=LAMINATE_CODE, site="Дуб Ява", size="1290x190x8",
                 name="MOST FLOOR Ле Паркет Дуб Ява"),
        ]

    def written(self, onec):
        names = {}
        for pack in onec.writes:
            for op in pack:
                if op.get("name"):
                    names[op.get("ref") or op.get("$id")] = op["name"]
        return names

    async def test_each_type_gets_its_own_template(self):
        onec = FakeOnec(self.mixed())
        status, text = await run_normalization(
            onec, mark_task(), allow)              # задача на марку целиком
        names = self.written(onec)

        # Вид товара стоит ПЕРВЫМ и у каждого свой (§19.5).
        self.assertTrue(names["C1"].startswith("Керамическая плитка"), names["C1"])
        self.assertTrue(names["L1"].startswith("Ламинат"), names["L1"])

    async def test_size_goes_into_the_name_only_for_ceramics(self):
        """Ровно то, ради чего развилка и нужна: у керамики в одной коллекции лежат
        форматы 30x60 и 60x60, у ламината формат один на всю коллекцию."""
        onec = FakeOnec(self.mixed())
        await run_normalization(onec, mark_task(), allow)
        names = self.written(onec)

        self.assertIn("60x60", names["C1"])
        self.assertNotIn("1290x190x8", names["L1"])

    async def test_two_types_in_one_collection_do_not_mix(self):
        """Одно имя коллекции у двух видов товара — две группы, а не одна: иначе вид
        товара первого попал бы в имена второго."""
        onec = FakeOnec([
            item(ref="A1", collection="Гранд", product_type="Ламинат",
                 product_type_ref=LAMINATE_CODE, site="Дуб"),
            item(ref="A2", collection="Гранд", product_type="Керамогранит",
                 product_type_ref=CERAMIC_CODE, site="Беж"),
        ])
        await run_normalization(onec, task(collection="Гранд"), allow)
        names = self.written(onec)

        self.assertTrue(names["A1"].startswith("Ламинат"), names["A1"])
        self.assertTrue(names["A2"].startswith("Керамогранит"), names["A2"])


class FolderSizeTest(unittest.IsolatedAsyncioTestCase):
    """Размер из имени ПАПКИ не должен протекать в наименование ТОВАРА.

    СЛУЧАЙ С БОЯ (21.09.2026, A+ Floor). Свойство «Коллекция» у всех двадцати позиций
    пустое, папка называется «Ле Паркет 600x600x14» — по §19.5 так и надо, у восьми
    напольных категорий размер пишется в имя папки. Подставив имя папки как коллекцию,
    нормализация выдала «Ламинат A+ Floor Ле Паркет 600x600x14 Авила»: размер оказался в
    СЕРЕДИНЕ наименования, хотя в имя товара он идёт только у керамики.
    """

    def real(self, site="Авила", name=None):
        """Позиция ровно в том виде, в каком её отдала боевая 1С."""
        return item(ref="A1", article="", site=site, collection="",
                    product_type="Ламинат", product_type_ref="000000003",
                    size="600x600x14",
                    name=name or f"Ламинат A+ Floor Ле Паркет {site}")

    def setUp(self):
        self.items = [self.real()]
        # Поле `parent` у помощника равно коллекции, а здесь нужна ИМЕННО папка с размером.
        object.__setattr__(self.items[0], "parent", "Ле Паркет 600x600x14")

    def test_collection_loses_the_folder_size(self):
        from src.model.normalize import collection_of
        self.assertEqual(collection_of(self.items[0]), "Ле Паркет")

    def test_filled_property_wins_over_the_folder(self):
        """Свойство заполнено — берём его, без всякой реконструкции."""
        from src.model.normalize import collection_of
        good = item(collection="Vintage", size="1290x190x8")
        object.__setattr__(good, "parent", "Vintage 1290x190x8")
        self.assertEqual(collection_of(good), "Vintage")

    async def test_size_does_not_leak_into_the_name(self):
        onec = FakeOnec(self.items, tm="A+ Floor")
        await run_normalization(onec, mark_task(), allow)

        names = [op.get("name") for pack in onec.writes for op in pack if op.get("name")]
        for written in names:
            self.assertNotIn("600x600x14", written,
                             "размер папки протёк в наименование товара")

    async def test_already_broken_names_are_repaired(self):
        """Соседи, у которых размер уже вписан в имя, должны ПОЧИНИТЬСЯ, а не задать
        образец для остальных."""
        broken = self.real(site="Тироль",
                           name="Ламинат A+ Floor Ле Паркет 600x600x14 Тироль")
        object.__setattr__(broken, "parent", "Ле Паркет 600x600x14")
        object.__setattr__(broken, "ref", "A2")

        onec = FakeOnec(self.items + [broken], tm="A+ Floor")
        await run_normalization(onec, mark_task(), allow)

        written = {op["ref"]: op["name"]
                   for pack in onec.writes for op in pack if op.get("name")}
        self.assertEqual(written.get("A2"), "Ламинат A+ Floor Ле Паркет Тироль")


class DiscontinueTest(unittest.IsolatedAsyncioTestCase):
    """Коллекция уходит в снятые ПАПКОЙ, одной операцией.

    СЛУЧАЙ С БОЯ (21.09.2026). В инструментах агента не было переноса папки, и он двигал
    позиции по одной: восемь вызовов там, где хватало одного, а пустая папка осталась
    висеть в живой ветке. Решать здесь нечего — папка известна, цель выводится из вида
    товара, — поэтому модель тут не участвует.
    """

    LAMINATE = "000000003"        # вид товара «Ламинат»
    TARGET = "YO-00006107"        # его папка снятых по `discontinued.MAPPING`

    class Folder:
        def __init__(self, ref, name, kind="collection", not_exported=False,
                     product_type_ref="000000003"):
            self.ref = ref
            self.name = name
            self.kind = kind
            self.parent_ref = "TM1"
            self.level = 2
            self.not_exported = not_exported
            self.product_type_ref = product_type_ref

    class Tree:
        def __init__(self, items):
            self.items = list(items)
            self.total = len(self.items)
            self.errors = []

    def onec(self, items, folders):
        outer = self

        class Fake(FakeOnec):
            def __init__(self):
                super().__init__(items)
                self.ops = None

            def folders(self, **kw):
                return outer.Tree(folders)

            def set_items(self, ops):
                self.ops = ops
                return {"created": 0, "updated": len(ops), "errors": []}

        return Fake()

    def task_for(self, name="Brilliant", code=""):
        mark = Ref.make(code="000000311", names=["Most Flooring"])
        return PriceTask(kind=TaskKind.MOVE_DISCONTINUED,
                         address=TaskAddress(tm=mark,
                                             subject=Ref.make(code=code, names=[name]),
                                             subject_kind=TaskSubject.COLLECTION),
                         description="проверить", id=9)

    def item_in(self, ref, collection="Brilliant", folder="F-BR", dead=False):
        it = item(ref=ref, collection=collection, product_type="Ламинат",
                  product_type_ref=self.LAMINATE, not_exported=dead)
        object.__setattr__(it, "collection_ref", folder)
        return it

    async def test_one_operation_moves_the_whole_folder(self):
        onec = self.onec([self.item_in("R1"), self.item_in("R2")],
                         [self.Folder("F-BR", "Коллекция Brilliant - 10 декоров")])
        status, text = await run_discontinue(onec, self.task_for(code="F-BR"), allow)

        self.assertEqual(onec.ops, [{"op": "update_folder", "ref": "F-BR",
                                     "parent_ref": self.TARGET}])
        self.assertEqual(status, TaskStatus.DONE)
        self.assertIn("2 позиц", text)

    async def test_empty_folder_still_moves(self):
        """Позиции могли уехать в снятые поодиночке раньше — папка остаётся живой, и
        убрать её всё равно надо. Ровно это и было с Brilliant."""
        onec = self.onec([self.item_in("R1", dead=True)],
                         [self.Folder("F-BR", "Коллекция Brilliant - 10 декоров")])
        status, text = await run_discontinue(onec, self.task_for(code="F-BR"), allow)
        self.assertIsNotNone(onec.ops)
        self.assertIn("сняты раньше", text)

    async def test_already_discontinued_folder_is_left_alone(self):
        onec = self.onec([], [self.Folder("F-BR", "Brilliant", not_exported=True)])
        status, text = await run_discontinue(onec, self.task_for(code="F-BR"), allow)
        self.assertIsNone(onec.ops)
        self.assertEqual(status, TaskStatus.DONE)

    async def test_shared_folder_is_refused(self):
        """Перенос утащил бы соседей: позиции без своей папки лежат прямо в папке марки."""
        onec = self.onec([self.item_in("R1"), self.item_in("R2", collection="Accord")],
                         [self.Folder("F-BR", "Общая папка")])
        status, text = await run_discontinue(onec, self.task_for(code="F-BR"), allow)
        self.assertIsNone(onec.ops)
        self.assertEqual(status, TaskStatus.TODO)
        self.assertIn("другие коллекции", text)

    async def test_folder_found_by_name_when_the_task_has_no_code(self):
        """Задачи прежней версии кода папки не несут — ищем по имени как по слову."""
        onec = self.onec([self.item_in("R1")],
                         [self.Folder("F-BR", "Коллекция Brilliant - 10 декоров")])
        status, _ = await run_discontinue(onec, self.task_for(), allow)
        self.assertEqual(onec.ops[0]["ref"], "F-BR")

    async def test_ambiguous_name_moves_nothing(self):
        onec = self.onec([self.item_in("R1")],
                         [self.Folder("F1", "Brilliant"),
                          self.Folder("F2", "Brilliant Plus")])
        status, text = await run_discontinue(onec, self.task_for(), allow)
        self.assertIsNone(onec.ops)
        self.assertEqual(status, TaskStatus.TODO)

    async def test_guard_stops_the_move(self):
        from src.model.executor import WriteRefused

        def deny():
            raise WriteRefused("захват потерян")

        onec = self.onec([self.item_in("R1")],
                         [self.Folder("F-BR", "Brilliant")])
        with self.assertRaises(WriteRefused):
            await run_discontinue(onec, self.task_for(code="F-BR"), deny)
        self.assertIsNone(onec.ops)


class CollectionPropertyTest(unittest.TestCase):
    """Пустое свойство «Коллекция» НЕ заполняется именем папки.

    Папка и свойство — разные вещи. Имя папки несёт размер (§19.5) и меняется при
    пересортировке справочника; свойство попадает на сайт как признак коллекции.
    Переписать одно другим значит подменить справочные данные производной величиной,
    выведенной ради сборки строки.
    """

    def test_normalization_never_writes_properties_at_all(self):
        """У нормализации в правке нет свойств вовсе — ей нечего там менять."""
        inputs, _, _, _ = nz.plan([item()], "000000311", "Most Flooring")
        self.assertNotIn("properties", inputs[0])

    def test_collection_property_is_stripped_from_a_write(self):
        """Свойства лежат ВНУТРИ позиции: фильтр, смотрящий на верхний уровень, пропустил
        бы их все — так и было в первой редакции."""
        inp = {"items": [{"op": "update", "ref": "T1", "properties": [
            {"property": nz.COLLECTION_PROPERTY, "value_code": "V1"},
            {"property": "0000007", "value_code": "V2"}]}]}
        out, note = nz.strip_collection_property(inp)
        self.assertEqual([p["property"] for p in out["items"][0]["properties"]],
                         ["0000007"])
        self.assertIn("Коллекция", note)

    def test_other_properties_pass_untouched(self):
        inp = {"items": [{"op": "update", "ref": "T1",
                          "properties": [{"property": "0000007", "value_code": "V2"}]}]}
        out, note = nz.strip_collection_property(inp)
        self.assertEqual(out["items"][0]["properties"], inp["items"][0]["properties"])
        self.assertEqual(note, "")

    def test_only_the_offending_rows_are_copied(self):
        """Позиции без нарушения остаются теми же объектами: правка касается лишь тех,
        кого она правда касается."""
        clean = {"op": "update", "ref": "T2",
                 "properties": [{"property": "0000007"}]}
        inp = {"items": [
            {"op": "update", "ref": "T1",
             "properties": [{"property": nz.COLLECTION_PROPERTY}]},
            clean,
        ]}
        out, _ = nz.strip_collection_property(inp)
        self.assertIs(out["items"][1], clean)

    def test_write_without_properties_is_left_alone(self):
        inp = {"tm_code": "T1", "collection": "Vintage"}
        out, note = nz.strip_collection_property(inp)
        self.assertIs(out, inp)
        self.assertEqual(note, "")


class ReportTest(unittest.TestCase):

    def test_silent_work_is_one_line(self):
        text = nz.report(12, [], 0, 1)
        self.assertIn("12 поз.", text)
        self.assertNotIn("ТРЕБУЕТ", text)

    def test_nothing_to_do_says_so(self):
        self.assertIn("менять было нечего", nz.report(0, [], 0, 0))

    def test_admin_sees_only_what_needs_deciding(self):
        skipped = [(item(article="3309"), nz.Skipped.NO_TITLE),
                   (item(article="1001"), nz.Skipped.DIRTY_TITLE)]
        text = nz.report(5, skipped, 3, 1)
        self.assertIn("ТРЕБУЕТ ВАШЕГО РЕШЕНИЯ", text)
        self.assertIn("3309", text)
        self.assertIn("1001", text)
        self.assertIn("Снятые с производства не трогал: 3", text)

    def test_long_lists_are_trimmed(self):
        skipped = [(item(article=str(i)), nz.Skipped.NO_TITLE) for i in range(20)]
        text = nz.report(0, skipped, 0, 0)
        self.assertIn("и ещё 12", text)


class RunTest(unittest.IsolatedAsyncioTestCase):
    """Прогон целиком — без единого обращения к модели."""

    async def test_writes_and_reports(self):
        onec = FakeOnec([item(), item(ref="T2", article="1001", site="Дуб Ява")])
        status, text = await run_normalization(onec, task(), allow)
        self.assertTrue(onec.writes, "запись не состоялась: %s" % text)
        self.assertEqual(status, TaskStatus.DONE)
        self.assertIn("приведены к шаблону", text)

    async def test_brand_in_written_names_is_the_1c_one(self):
        """Имя в 1С собрано с «MOST FLOOR» из прайса — после нормализации должно стать
        «Most Flooring», как записана марка."""
        onec = FakeOnec([item()])
        await run_normalization(onec, task(), allow)
        names = [o.get("name", "") for o in onec.writes[0] if o.get("name")]
        self.assertTrue(names)
        self.assertIn("Most Flooring", names[0])
        self.assertNotIn("MOST FLOOR", names[0])

    async def test_partial_when_something_was_left_to_the_admin(self):
        onec = FakeOnec([item(), item(ref="T2", site="")])
        status, text = await run_normalization(onec, task(), allow)
        self.assertEqual(status, TaskStatus.PARTIAL)
        self.assertIn("ТРЕБУЕТ", text)

    async def test_guard_stops_the_write(self):
        from src.model.executor import WriteRefused

        def deny():
            raise WriteRefused("захват потерян")

        onec = FakeOnec([item()])
        with self.assertRaises(WriteRefused):
            await run_normalization(onec, task(), deny)
        self.assertEqual(onec.writes, [])

    async def test_task_without_a_mark_code_is_refused(self):
        onec = FakeOnec([item()])
        bad = task()
        bad.address = TaskAddress(tm=Ref.make(names=["Most Flooring"]),
                                  subject=Ref.make(names=["Миллениум Про"]))
        status, text = await run_normalization(onec, bad, allow)
        self.assertEqual(status, TaskStatus.TODO)
        self.assertEqual(onec.writes, [])
        self.assertIn("нет кода марки", text)

    async def test_no_llm_is_involved(self):
        """Ни оркестратора, ни ключа модели: путь обязан работать без них вовсе."""
        onec = FakeOnec([item()])
        status, _ = await run_normalization(onec, task(), allow)
        self.assertIn(status, (TaskStatus.DONE, TaskStatus.PARTIAL))


if __name__ == "__main__":
    unittest.main()
