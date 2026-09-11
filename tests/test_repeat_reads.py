"""Повторные справочные чтения не пускаются в историю (§9.6.5).

Папки марки и свойства вида товара за прогон не меняются, но выгрузки тяжёлые — свойства
одного вида до ~8 тыс. токенов. И, в отличие от листов прайса и номенклатуры,
`_prune_file_dumps` их НЕ чистит: повторная выгрузка осядет в переписке до конца прогона и
поедет в КАЖДОМ следующем запросе цикла.

Круг цикла этим не вернуть — он уже оплачен к моменту, когда инструмент отвечает. Круг
убирает запрет в промпте; здесь закрывается вторая половина: чтобы даже нарушенный запрет
не стоил ничего сверх самого круга.

После записи в 1С право переспросить ОБЯЗАНО вернуться: `set_items` заводит папки и
добавляет значения свойств — ровно те данные, что мы запретили перечитывать.
"""
import json
import tempfile
import unittest
from pathlib import Path

from src.agent.pricing_tools import PricingTools, clear_nomenclature_cache
from src.storage.pricing import PricingStore
from tests.test_item_tools import TM, FakeOnec


class Base(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec([])
        self.tools = PricingTools(self.onec, self.store, 42)

    async def asyncTearDown(self):
        clear_nomenclature_cache()
        self._dir.cleanup()

    async def folders(self, **inp):
        return json.loads(await self.tools.execute("get_1c_folders", inp))

    async def props(self, **inp):
        return json.loads(await self.tools.execute("get_1c_properties", inp))


class FoldersTest(Base):

    async def test_first_call_returns_the_tree(self):
        out = await self.folders(tm=TM)
        self.assertEqual(len(out["folders"]), 2)

    async def test_second_identical_call_returns_a_pointer(self):
        await self.folders(tm=TM)
        out = await self.folders(tm=TM)
        self.assertTrue(out["repeat"])
        self.assertNotIn("folders", out)
        self.assertIn("выше в переписке", out["hint"])

    async def test_pointer_says_what_was_returned(self):
        """Напоминание должно быть конкретным, иначе модель ему не поверит."""
        await self.folders(tm=TM)
        self.assertIn("2", (await self.folders(tm=TM))["was"])

    async def test_pointer_is_far_smaller_than_a_real_dump(self):
        """На боевой базе дерево — около 1 290 узлов; ради экономии всё и затевалось."""
        from src.onec.client import Folder, FolderTree

        self.onec.folders = lambda product_type=None, tm=None: FolderTree(
            total=1290, items=[
                Folder(f"YO-{i:08}", f"Коллекция Дуб Верона {i}", "YO-00002590",
                       "collection", 4, False, False, "000000002", TM, 0.0)
                for i in range(1290)])

        full = await self.tools.execute("get_1c_folders", {"tm": TM})
        again = await self.tools.execute("get_1c_folders", {"tm": TM})
        self.assertGreater(len(full), 100_000)          # выгрузка действительно тяжёлая
        self.assertLess(len(again), len(full) / 100)

    async def test_different_filters_are_different_questions(self):
        await self.folders(tm=TM)
        out = await self.folders(product_type="000000002")
        self.assertNotIn("repeat", out)

    async def test_write_to_1c_restores_the_right_to_ask(self):
        """set_items заводит папки — запрет на перечитывание после этого неверен."""
        await self.folders(tm=TM)
        clear_nomenclature_cache()          # так делает обработчик после записи
        self.assertEqual(len((await self.folders(tm=TM))["folders"]), 2)


class PropertiesTest(Base):

    async def test_second_identical_call_returns_a_pointer(self):
        await self.props(product_type="000000002")
        out = await self.props(product_type="000000002")
        self.assertTrue(out["repeat"])
        self.assertNotIn("properties", out)

    async def test_pointer_names_the_product_type(self):
        await self.props(product_type="000000002")
        self.assertIn("Виниловый ламинат", (await self.props(product_type="000000002"))["was"])

    async def test_narrowed_request_is_not_blocked_by_the_broad_one(self):
        """«Коллекция» по марке — другой вопрос, чем все свойства вида товара."""
        await self.props(product_type="000000002")
        out = await self.props(product_type="000000002", tm=TM, property="0000003")
        self.assertNotIn("repeat", out)
        self.assertIn("properties", out)

    async def test_1c_is_not_called_twice(self):
        calls = []
        original = self.onec.properties_by_type

        def counting(*a, **kw):
            calls.append(a)
            return original(*a, **kw)

        self.onec.properties_by_type = counting
        await self.props(product_type="000000002")
        await self.props(product_type="000000002")
        self.assertEqual(len(calls), 1)


class IsolationTest(Base):
    """Отметки живут ровно один прогон: новый прайс начинает с чистого листа."""

    async def test_new_price_run_starts_fresh(self):
        await self.folders(tm=TM)
        clear_nomenclature_cache()          # так делает handle_price_document
        self.assertNotIn("repeat", await self.folders(tm=TM))


if __name__ == "__main__":
    unittest.main()
