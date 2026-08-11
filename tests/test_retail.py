"""Юнит-тесты расчёта розничной цены (specs/retail-price-rules.md), без сети."""
import unittest
from datetime import date
from decimal import Decimal

from src.price_tool.retail import compute_retail, markup_pct

TODAY = date(2026, 8, 11)
D = Decimal


class MarkupTiersTest(unittest.TestCase):
    def test_tiers_and_boundaries(self):
        self.assertEqual(markup_pct(D("999")), D("20"))
        self.assertEqual(markup_pct(D("1000")), D("15"))      # ровно 1000 → 15%
        self.assertEqual(markup_pct(D("2000")), D("15"))      # ровно 2000 → 15%
        self.assertEqual(markup_pct(D("2000.01")), D("12"))


class RetailComputeTest(unittest.TestCase):
    def test_rounds_to_ruble(self):
        d = compute_retail(D("999"), today=TODAY)
        self.assertEqual(d.value, D("1199"))                  # 1198.80 → 1199
        self.assertTrue(d.write)

    def test_capped_by_fresh_rrc(self):
        d = compute_retail(D("1500"), rrc=D("1600"), rrc_date=date(2026, 8, 1), today=TODAY)
        self.assertEqual(d.value, D("1600"))                  # 1725 → обрезано
        self.assertTrue(d.capped)

    def test_no_cap_when_rrc_stale(self):
        d = compute_retail(D("1200"), rrc=D("1220.99"), rrc_date=date(2019, 5, 1), today=TODAY)
        self.assertEqual(d.value, D("1380"))
        self.assertFalse(d.capped)

    def test_no_cap_when_rrc_missing(self):
        self.assertEqual(compute_retail(D("1000"), today=TODAY).value, D("1150"))

    def test_rrc_below_purchase_warns_and_keeps_calculated(self):
        d = compute_retail(D("1200"), rrc=D("1150"), rrc_date=date(2026, 8, 1), today=TODAY)
        self.assertEqual(d.value, D("1380"))                  # не обрезаем — §4
        self.assertFalse(d.capped)
        self.assertEqual(d.warning, "rrc_below_purchase")

    def test_threshold_blocks_small_change(self):
        d = compute_retail(D("912.50"), current_retail=D("1080"), today=TODAY)
        self.assertEqual(d.value, D("1095"))                  # +1.39% → не пишем
        self.assertFalse(d.write)
        self.assertEqual(d.reason, "below_threshold")

    def test_threshold_allows_two_percent(self):
        d = compute_retail(D("918.34"), current_retail=D("1080"), today=TODAY)
        self.assertEqual(d.value, D("1102"))                  # +2.04% → пишем
        self.assertTrue(d.write)

    def test_threshold_symmetric_for_decrease(self):
        d = compute_retail(D("881.67"), current_retail=D("1080"), today=TODAY)
        self.assertEqual(d.value, D("1058"))                  # −2.04% → пишем
        self.assertTrue(d.write)

    def test_first_time_ignores_threshold(self):
        d = compute_retail(D("900"), current_retail=None, today=TODAY)
        self.assertTrue(d.write)
        self.assertEqual(d.reason, "first_time")

    def test_purchase_unchanged_skips(self):
        d = compute_retail(D("900"), current_retail=D("1080"), today=TODAY,
                           purchase_changed=False)
        self.assertIsNone(d.value)
        self.assertFalse(d.write)
        self.assertEqual(d.reason, "purchase_unchanged")

    def test_no_purchase(self):
        self.assertEqual(compute_retail(None, today=TODAY).reason, "no_purchase")


if __name__ == "__main__":
    unittest.main()
