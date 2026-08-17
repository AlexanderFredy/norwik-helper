"""Подозрительные скачки цен по коллекции и попытка назвать причину (§9.1.1).

Боевой случай 14.08.2026 оказался НЕ ошибкой единицы измерения, хотя выглядел ею:
закупка Classen Euphoria «выросла» с 880 до 1775, и отношение 2.02 почти совпало с
коэффициентом упаковки 1.974. На деле 1775 стояло в 1С с 15.05 у пяти позиций из шести,
а 880 было у одной — «Euphoria WR Дуб Саттон 58», другого товара в той же папке.

Отсюда порядок проверок: сначала то, что видно по самой коллекции (часть позиций уже
стоит новую цену; цены внутри разные), и только для однородной коллекции — гипотеза про
упаковку. Иначе совпадение отношения выдаётся за диагноз.
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
        self.assertIn("было 880", text)
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


class MixedCollectionTest(unittest.TestCase):
    """Разные товары в одной папке — реальный Classen, а не перепутанная ЕИ."""

    def _euphoria(self):
        items = [item(f"YO-7074{i}", 1775, 1.974) for i in range(5)]
        items.append(item("YO-00076843", 880, 1.974))
        items[-1].__dict__["name"] = "Ламинат Classen Euphoria WR Дуб Саттон FSC 58"
        return plan_collection(items, "T", "Classen", Decimal("1775"), None,
                               date(2026, 8, 14))

    def test_outlier_named_not_blamed_on_packaging(self):
        [text] = unit_warnings(self._euphoria())
        self.assertIn("5 из 6 поз. уже стоят столько же", text)
        self.assertIn("Дуб Саттон", text)
        self.assertIn("(880)", text)
        self.assertIn("РАЗНЫЕ товары", text)
        self.assertNotIn("ЗА УПАКОВКУ", text)      # главное: причину не выдумываем

    def test_spread_inside_collection(self):
        """Боевая WR: цены 864…1560, из прайса одна 1604 — папка неоднородна."""
        items = [item(f"YO-724{i}", 1560, 1.974) for i in range(4)]
        items += [item("YO-00074523", 900, 1.974), item("YO-00074524", 864, 1.974)]
        [text] = unit_warnings(plan_collection(items, "T", "Classen", Decimal("1604"),
                                               None, date(2026, 8, 14)))
        self.assertIn("цены разные (864…1 560)", text)
        self.assertIn("2 поз.", text)
        self.assertNotIn("ЗА УПАКОВКУ", text)

    def test_uniform_collection_still_gets_pack_hypothesis(self):
        items = [item(f"YO-{i}", 880, 1.974) for i in range(5)]
        [text] = unit_warnings(plan_collection(items, "T", "Classen", Decimal("1775"),
                                               None, date(2026, 8, 14)))
        self.assertIn("ЗА УПАКОВКУ", text)

    def test_all_positions_already_at_price_is_silent(self):
        """Нечего писать — нечего и предупреждать."""
        items = [item(f"YO-{i}", 1775, 1.974) for i in range(5)]
        self.assertEqual(unit_warnings(plan_collection(
            items, "T", "Classen", Decimal("1775"), None, date(2026, 8, 14))), [])


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

    def test_one_line_per_price_kind(self):
        """Коллекция описывается одной строкой, а не строкой на каждый товар."""
        items = [item("YO-1", 864, 1.974), item("YO-2", 900, 1.974)]
        group = plan_collection(items, "T", "Classen", Decimal("1604"), None,
                                date(2026, 8, 14))
        self.assertEqual(len(unit_warnings(group)), 1)


if __name__ == "__main__":
    unittest.main()
