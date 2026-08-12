"""Юнит-тесты решения «что писать в 1С» (src/price_tool/changes.py), без сети."""
import unittest
from datetime import date
from decimal import Decimal as D

from src.onec.client import NomItem, Price
from src.price_tool.changes import (build_payload, plan_collection, plan_item, significant)

TODAY = date(2026, 8, 11)


def item(ref="YO-1", purchase=None, rrc=None, retail=None, coll="YO-C", name="Товар"):
    def p(v, dt="2026-08-01"):
        return Price(value=float(v), date=dt) if v is not None else None
    return NomItem(ref=ref, id="1", name=name, article="", unit="м2", size="", product_type="Ламинат",
                   collection="Vintage", parent="Vintage", collection_ref=coll, alt_units={},
                   purchase=p(purchase), retail=p(retail), rrc=p(rrc))


class CollectionNameTest(unittest.TestCase):
    def test_empty_collection_falls_back_to_parent(self):
        """У ~130 товаров каталога реквизит «Коллекция» пуст — админу нужно имя папки."""
        bare = NomItem(ref="YO-1", id="1", name="Паркетная доска Farecom", article="",
                       unit="м2", size="", product_type="Паркетная доска", collection="",
                       parent="Farecom", collection_ref="YO-F", alt_units={},
                       purchase=Price(500.0, "2026-01-01"), retail=None, rrc=None)
        g = plan_collection([bare], "T", "Farecom", D("600"), None, TODAY)
        self.assertEqual(g.collection, "Farecom")

    def test_collection_wins_over_parent(self):
        g = plan_collection([item(purchase=500)], "T", "TM", D("600"), None, TODAY)
        self.assertEqual(g.collection, "Vintage")


class ThresholdTest(unittest.TestCase):
    def test_below_threshold_not_written(self):
        self.assertFalse(significant(D("1070"), D("1069.93")))     # 0.007%
        self.assertFalse(significant(D("1585"), D("1585.08")))

    def test_at_threshold_written(self):
        self.assertTrue(significant(D("1000"), D("1020")))          # ровно 2%
        self.assertTrue(significant(D("1000"), D("980")))           # по модулю

    def test_first_time_always_written(self):
        self.assertTrue(significant(None, D("100")))

    def test_zero_or_missing_new(self):
        self.assertFalse(significant(D("100"), None))
        self.assertFalse(significant(D("100"), D("0")))


class PlanItemTest(unittest.TestCase):
    def test_kopeck_change_skips_everything(self):
        p = plan_item(item(purchase=1070, rrc=1585, retail=1230), D("1069.93"), D("1585.08"), TODAY)
        self.assertEqual(p.prices, {})
        self.assertEqual(p.skipped["purchase"], "below_threshold")
        self.assertEqual(p.skipped["rrc"], "below_threshold")
        # закупку не пишем → розницу не пересчитываем
        self.assertEqual(p.skipped["retail"], "purchase_unchanged")

    def test_real_change_writes_purchase_and_retail(self):
        p = plan_item(item(purchase=1255, rrc=1850, retail=1443.25), D("1200"), D("1750"), TODAY)
        self.assertEqual(p.prices["purchase"], D("1200"))
        self.assertEqual(p.prices["rrc"], D("1750"))
        self.assertEqual(p.prices["retail"], D("1380"))            # 1200 × 1.15

    def test_first_price_ever(self):
        p = plan_item(item(purchase=None, rrc=None, retail=None), D("900"), D("2070"), TODAY)
        self.assertEqual(p.prices["purchase"], D("900"))
        self.assertEqual(p.prices["rrc"], D("2070"))
        self.assertEqual(p.prices["retail"], D("1080"))

    def test_rrc_below_purchase_signals_and_does_not_cap(self):
        p = plan_item(item(purchase=1000, rrc=2000, retail=1150), D("1200"), D("1150"), TODAY)
        self.assertEqual(p.prices["retail"], D("1380"))            # обрезки нет
        self.assertEqual(p.warning, "rrc_below_purchase")

    def test_rrc_only_change_does_not_touch_retail(self):
        p = plan_item(item(purchase=1000, rrc=1500, retail=1150), D("1000"), D("1900"), TODAY)
        self.assertIn("rrc", p.prices)
        self.assertNotIn("retail", p.prices)
        self.assertNotIn("purchase", p.prices)


class PayloadTest(unittest.TestCase):
    def _collection(self, purchases):
        return [item(ref=f"YO-{i}", purchase=v, rrc=2000, retail=None)
                for i, v in enumerate(purchases)]

    def test_uniform_collection_uses_form_a(self):
        g = plan_collection(self._collection([900, 900, 900]), "000000311", "Most",
                            D("1000"), D("2000"), TODAY)
        payload = build_payload([g])
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["collection_ref"], "YO-C")
        self.assertNotIn("ref", payload[0])

    def test_mixed_collection_falls_back_to_form_b(self):
        # у одного товара цена уже равна новой → пишем не всю коллекцию, а по товарам
        g = plan_collection(self._collection([900, 1000, 900]), "000000311", "Most",
                            D("1000"), D("2000"), TODAY)
        payload = build_payload([g])
        self.assertEqual(len(payload), 2)
        self.assertTrue(all("ref" in p for p in payload))

    def test_nothing_to_write_gives_empty_payload(self):
        g = plan_collection(self._collection([1000, 1000]), "000000311", "Most",
                            D("1000"), D("2000"), TODAY)
        for p in g.plans:                       # РРЦ совпала, закупка совпала
            p.prices.pop("rrc", None)
        self.assertEqual(build_payload([g]), [])


if __name__ == "__main__":
    unittest.main()
