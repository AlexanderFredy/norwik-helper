"""Плитка: цены задаются поэлементно, а не одной на коллекцию (§7.7).

В одной коллекции плитки лежат настенная, напольная, декоры, бордюры — разное назначение,
разный размер, разная цена. Единая цена на папку тут неверна по существу, а групповая
форма запроса (§10.2 «а») задела бы соседние элементы с их собственными ценами.
"""
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from src.agent.pricing_tools import PricingTools, clear_nomenclature_cache
from src.onec.client import NomItem, Price
from src.price_tool.changes import build_payload, plan_items
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec

TM, COLL = "000000150", "YO-TILE"
TODAY = date(2026, 8, 27)


def tile(ref, name, size, purchase):
    return NomItem(ref=ref, id="1", name=name, article="", unit="м2", size=size,
                   product_type="Керамическая плитка", collection="Мадейра",
                   parent="Мадейра", collection_ref=COLL, alt_units={"упак": 1.44},
                   purchase=Price(value=purchase, date="2026-05-01"), retail=None, rrc=None)


def collection():
    return [
        tile("YO-1", "Мадейра настенная бежевая", "30x60", 900.0),
        tile("YO-2", "Мадейра напольная бежевая", "60x60", 1100.0),
        tile("YO-3", "Мадейра декор", "30x60", 2500.0),
        tile("YO-4", "Мадейра бордюр", "8x60", 400.0),
    ]


class PlanItemsTest(unittest.TestCase):
    def test_each_element_gets_its_own_price(self):
        group, missing = plan_items(collection(), TM, "Cersanit", [
            {"ref": "YO-1", "purchase": 990},
            {"ref": "YO-2", "purchase": 1210},
            {"ref": "YO-3", "purchase": 2750},
            {"ref": "YO-4", "purchase": 440},
        ], TODAY)
        self.assertEqual(missing, [])
        self.assertTrue(group.whole)
        self.assertEqual({p.ref: p.prices["purchase"] for p in group.plans},
                         {"YO-1": Decimal("990"), "YO-2": Decimal("1210"),
                          "YO-3": Decimal("2750"), "YO-4": Decimal("440")})

    def test_untouched_items_stay_out(self):
        """Элементов коллекции нет в прайсе — их цены трогать нельзя."""
        group, _ = plan_items(collection(), TM, "Cersanit",
                              [{"ref": "YO-1", "purchase": 990}], TODAY)
        self.assertEqual([p.ref for p in group.plans], ["YO-1"])
        self.assertFalse(group.whole)

    def test_unknown_ref_reported_not_silently_dropped(self):
        group, missing = plan_items(collection(), TM, "Cersanit", [
            {"ref": "YO-1", "purchase": 990}, {"ref": "YO-999", "purchase": 100},
        ], TODAY)
        self.assertEqual(missing, ["YO-999"])
        self.assertEqual(len(group.plans), 1)

    def test_partial_group_never_uses_collection_form(self):
        """Главная защита: одна цена на ЧАСТЬ папки не должна писаться на всю папку."""
        group, _ = plan_items(collection(), TM, "Cersanit", [
            {"ref": "YO-1", "purchase": 990}, {"ref": "YO-3", "purchase": 990},
        ], TODAY)
        payload = build_payload([group])
        self.assertEqual(len(payload), 2)
        self.assertTrue(all("ref" in row for row in payload))
        self.assertFalse(any("collection_ref" in row for row in payload))

    def test_whole_collection_with_one_price_may_use_group_form(self):
        group, _ = plan_items(collection(), TM, "Cersanit",
                              [{"ref": r, "purchase": 990}
                               for r in ("YO-1", "YO-2", "YO-3", "YO-4")], TODAY)
        payload = build_payload([group])
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["collection_ref"], COLL)

    def test_bad_price_is_ignored_not_crashing(self):
        group, _ = plan_items(collection(), TM, "Cersanit",
                              [{"ref": "YO-1", "purchase": "не число"}], TODAY)
        self.assertEqual(group.plans[0].prices, {})


class TileThroughToolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec(collection())
        self.tools = PricingTools(self.onec, self.store, user_id=42)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _propose(self, items):
        return await self.tools.execute("propose_prices", {
            "supplier": "Плиточник",
            "groups": [{"tm_code": TM, "tm_name": "Cersanit", "collection_ref": COLL,
                        "items": items}]})

    async def test_different_prices_in_one_collection_are_normal(self):
        text = await self._propose([
            {"ref": "YO-1", "purchase": 990}, {"ref": "YO-2", "purchase": 1210},
            {"ref": "YO-3", "purchase": 2750},
        ])
        self.assertIn("Мадейра", text)
        self.assertIn("К записи: 3 поз.", text)
        # разброс цен внутри коллекции плитки — норма, а не повод для предупреждения
        self.assertNotIn("цены разные", text)
        self.assertNotIn("ЗА УПАКОВКУ", text)

    async def test_missing_ref_lands_in_warnings(self):
        text = await self._propose([{"ref": "YO-1", "purchase": 990},
                                    {"ref": "YO-777", "purchase": 990}])
        self.assertIn("не нашёл в 1С 1 поз.", text)
        self.assertIn("YO-777", text)

    async def test_payload_is_per_item(self):
        await self._propose([{"ref": "YO-1", "purchase": 990},
                             {"ref": "YO-2", "purchase": 1210}])
        pending = await self.store.get_pending(42)
        self.assertTrue(all("ref" in row for row in pending.payload))

    async def test_collection_price_still_works_for_laminate(self):
        """Старый путь не сломан: у ламината коллекция однородна."""
        text = await self.tools.execute("propose_prices", {
            "supplier": "Монарх",
            "groups": [{"tm_code": TM, "tm_name": "Cersanit", "collection_ref": COLL,
                        "purchase": 990}]})
        self.assertIn("К записи:", text)


if __name__ == "__main__":
    unittest.main()
