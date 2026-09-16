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


def item(ref="T1", article="3309", site="Бах", collection="Миллениум Про",
         product_type="Ламинат", name="", size="", not_exported=False):
    return NomItem(
        ref=ref, id="", name=name or f"Ламинат MOST FLOOR {collection} {site}",
        article=article, unit="м2", size=size, product_type=product_type,
        collection=collection, parent=collection, collection_ref="F1", alt_units={},
        purchase=None, retail=None, rrc=None,
        full_name="", site_name=site, product_type_ref="PT1",
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
    return PriceTask(
        kind=TaskKind.NORMALIZE_NAMES,
        address=TaskAddress(tm=Ref.make(code=tm_code, names=["Most Flooring"]),
                            subject=Ref.make(names=[collection]),
                            subject_kind=TaskSubject.COLLECTION),
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
