"""Марка заведена, но ещё не помечена к выгрузке на сайт (§19.10).

Это рабочий промежуток, а не исключение: марку заводят раньше, чем помечают, — сначала её
прорабатывают, и товары под неё в это время уже создают. Раньше `get_selling_tm` отдавал
только помеченные, и агент честно писал «такой ТМ нет, заведите» про марку, заведённую
админом час назад: A+ FLOOR он не видел вовсе, а `create_item.manufacturer` требует её код.
"""
import json
import tempfile
import unittest
from pathlib import Path

from src.agent.pricing_tools import PricingTools, clear_nomenclature_cache
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec, item


class SellingTmTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec([item("YO-1", 949, 1649, 1139)])
        self.onec.tms = [("Egger", "T1"), ("A+ FLOOR", "T9", False)]
        self.tools = PricingTools(self.onec, self.store, 42)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _marks(self) -> list[dict]:
        return json.loads(await self.tools.execute("get_selling_tm", {}))

    async def test_unexported_mark_is_visible(self):
        names = [m["name"] for m in await self._marks()]
        self.assertIn("A+ FLOOR", names)

    async def test_flag_distinguishes_the_two(self):
        by_name = {m["name"]: m for m in await self._marks()}
        self.assertTrue(by_name["Egger"]["selling"])
        self.assertFalse(by_name["A+ FLOOR"]["selling"])

    async def test_code_is_available_for_create_item(self):
        """Ради этого кода всё и затевалось: create_item.manufacturer требует именно его."""
        by_name = {m["name"]: m for m in await self._marks()}
        self.assertEqual(by_name["A+ FLOOR"]["code"], "T9")

    async def test_client_still_filters_when_asked(self):
        """Сам клиент 1С по умолчанию отдаёт только помеченные — это не изменилось."""
        self.assertEqual([m.name for m in self.onec.selling_tm()], ["Egger"])
        self.assertEqual(len(self.onec.selling_tm(all_marks=True)), 2)


if __name__ == "__main__":
    unittest.main()
