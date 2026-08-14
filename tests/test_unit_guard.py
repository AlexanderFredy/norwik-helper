"""Подозрительные скачки цен и распознавание «цена за упаковку» (§9.1.1).

Боевой случай 14.08.2026: закупка Classen выросла с 880 до 1775 (+102%) — в 1С попала
цена за упаковку вместо цены за м². Предложение это показывало обычной строкой среди трёх
десятков коллекций, и ошибка прошла подтверждение.
"""
import unittest
from datetime import date
from decimal import Decimal

from src.onec.client import NomItem, Price
from src.price_tool.changes import plan_collection, unit_warnings


def item(ref: str, purchase: float | None, pack: float | None, coll="Euphoria",
         unit="м2") -> NomItem:
    return NomItem(
        ref=ref, id="1", name=f"Ламинат Classen {coll}", article="", unit=unit, size="",
        product_type="Ламинат", collection=coll, parent=coll, collection_ref="YO-C",
        alt_units={"упак": pack} if pack else {},
        purchase=Price(value=purchase, date="2026-01-13") if purchase else None,
        retail=None, rrc=None)


def warn(old, new, pack, unit="м2") -> list[str]:
    group = plan_collection([item("YO-1", old, pack, unit=unit)], "T", "Classen",
                            Decimal(str(new)), None, date(2026, 8, 14))
    return unit_warnings(group)


class PackMismatchTest(unittest.TestCase):
    def test_real_classen_case(self):
        [text] = warn(880, 1775, 1.974)
        self.assertIn("880 → 1 775 (+102%)", text)
        self.assertIn("ЗА УПАКОВКУ", text)
        self.assertIn("1.974", text)
        self.assertIn("899", text)              # сколько вышло бы за м²

    def test_names_the_base_unit(self):
        self.assertIn("а не за шт.", warn(100, 500, 5.0, unit="шт.")[0])

    def test_jump_unlike_pack_is_generic(self):
        """Скачок есть, но с упаковкой не бьётся — причину не выдумываем."""
        [text] = warn(700, 1500, 1.2)
        self.assertNotIn("ЗА УПАКОВКУ", text)
        self.assertIn("проверьте колонку прайса и единицу измерения", text)

    def test_no_pack_data_is_generic(self):
        [text] = warn(700, 1500, None)
        self.assertNotIn("ЗА УПАКОВКУ", text)


class QuietCasesTest(unittest.TestCase):
    def test_honest_increase_is_silent(self):
        """Egger +23% — обычное подорожание, шуметь не о чем."""
        self.assertEqual(warn(700, 858, 1.995), [])

    def test_decrease_within_threshold_is_silent(self):
        self.assertEqual(warn(1050, 919, 2.0), [])          # AGT −12%

    def test_big_drop_is_reported(self):
        """Деление на коэффициент дважды — такая же ошибка, только в другую сторону."""
        [text] = warn(1775, 880, 1.974)
        self.assertIn("-50%", text.replace("−", "-"))

    def test_first_price_is_silent(self):
        """Цены в 1С не было — сравнивать не с чем, скачка нет."""
        group = plan_collection([item("YO-1", None, 1.974)], "T", "Classen",
                                Decimal("1775"), None, date(2026, 8, 14))
        self.assertEqual(unit_warnings(group), [])

    def test_identical_transitions_reported_once(self):
        """Коллекция из 30 товаров с одним переходом — одна строка, а не тридцать."""
        items = [item(f"YO-{i}", 880, 1.974) for i in range(30)]
        group = plan_collection(items, "T", "Classen", Decimal("1775"), None,
                                date(2026, 8, 14))
        self.assertEqual(len(unit_warnings(group)), 1)

    def test_different_old_prices_reported_separately(self):
        """Боевая коллекция WR: 864 и 900 уезжают в 1604 — оба перехода подозрительны."""
        items = [item("YO-1", 864, 1.974), item("YO-2", 900, 1.974)]
        group = plan_collection(items, "T", "Classen", Decimal("1604"), None,
                                date(2026, 8, 14))
        self.assertEqual(len(unit_warnings(group)), 2)

    def test_small_move_within_same_group_stays_quiet(self):
        """У той же WR цена 1560 → 1604 — это +3%, порога скачка она не достигает."""
        items = [item("YO-1", 864, 1.974), item("YO-2", 1560, 1.974)]
        group = plan_collection(items, "T", "Classen", Decimal("1604"), None,
                                date(2026, 8, 14))
        texts = unit_warnings(group)
        self.assertEqual(len(texts), 1)
        self.assertIn("864", texts[0])


if __name__ == "__main__":
    unittest.main()
