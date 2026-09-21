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
from src.model.executor import run_normalization
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


class PlanTest(unittest.TestCase):
    """Что берём в работу, а что выносим админу."""

    def test_normal_items_are_planned(self):
        inputs, skipped, dropped = nz.plan([item(), item(ref="T2", article="1001",
                                                        site="Дуб Ява")],
                                           "000000311", "Most Flooring")
        self.assertEqual(len(inputs), 1)
        self.assertEqual(len(inputs[0]["items"]), 2)
        self.assertEqual(skipped, [])

    def test_title_comes_from_site_name(self):
        """`site_name` по §19.5 — это РОВНО название расцветки, готовый `title`."""
        inputs, _, _ = nz.plan([item(site="Дуб Ява")], "000000311", "Most Flooring")
        self.assertEqual(inputs[0]["items"][0]["title"], "Дуб Ява")

    def test_brand_is_the_1c_one(self):
        inputs, _, _ = nz.plan([item()], "000000311", "Most Flooring")
        self.assertEqual(inputs[0]["tm_name"], "Most Flooring")

    def test_item_without_a_title_goes_to_the_admin(self):
        """Название расцветки выводить неоткуда — выдумывать код не будет."""
        inputs, skipped, _ = nz.plan([item(site="")], "000000311", "Most Flooring")
        self.assertEqual(inputs, [])
        self.assertEqual(skipped[0][1], nz.Skipped.NO_TITLE)

    def test_contaminated_title_goes_to_the_admin(self):
        """Марка внутри названия расцветки попала бы в имя дважды."""
        inputs, skipped, _ = nz.plan([item(site="Most Flooring Бах")],
                                     "000000311", "Most Flooring")
        self.assertEqual(inputs, [])
        self.assertEqual(skipped[0][1], nz.Skipped.DIRTY_TITLE)

    def test_collection_inside_the_title_is_caught_too(self):
        inputs, skipped, _ = nz.plan([item(site="Миллениум Про Бах")],
                                     "000000311", "Most Flooring")
        self.assertEqual(skipped[0][1], nz.Skipped.DIRTY_TITLE)

    def test_short_words_do_not_block_normalization(self):
        """«Про» или «Ле» встречаются внутри расцветок как обычные слова: запрет по ним
        выключил бы нормализацию целым коллекциям."""
        inputs, skipped, _ = nz.plan([item(site="Про Бах", collection="Ле")],
                                     "000000311", "Most Flooring")
        self.assertEqual(skipped, [])
        self.assertEqual(len(inputs), 1)

    def test_discontinued_are_left_alone(self):
        """Снятые лежат в невыгружаемых папках, на сайт не идут — переименование им
        ничего не даёт, а пачку записи раздувает."""
        inputs, skipped, dropped = nz.plan(
            [item(), item(ref="T2", not_exported=True)], "000000311", "Most Flooring")
        self.assertEqual(dropped, 1)
        self.assertEqual(len(inputs[0]["items"]), 1)
        self.assertEqual(skipped, [])

    def test_only_collection_narrows_the_work(self):
        inputs, _, _ = nz.plan(
            [item(collection="Миллениум Про"), item(ref="T2", collection="Ле Паркет")],
            "000000311", "Most Flooring", only_collection="Ле Паркет")
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["collection"], "Ле Паркет")

    def test_groups_split_by_collection_and_type(self):
        """`plan_collection` работает на одну коллекцию и вид — бить обязаны мы."""
        inputs, _, _ = nz.plan(
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
