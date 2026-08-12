"""«Когда меняли цены» (src/price_tool/history.py): даты из 1С, источник — из журнала."""
import unittest

from src.onec.client import NomItem, Price
from src.price_tool.history import (describe_group, describe_product, dominant, fmt_date)


def item(ref="YO-1", name="Classen Adventure Дуб Авола", purchase=None, rrc=None,
         retail=None, dates=None, coll="Adventure"):
    """dates — {вид цены: ГГГГ-ММ-ДД}; по умолчанию все цены от одной даты."""
    dates = dates or {}
    default = "2026-07-20"

    def p(value, kind):
        return Price(value=float(value), date=dates.get(kind, default)) if value else None

    return NomItem(ref=ref, id="1", name=name, article="62593", unit="м2", size="",
                   product_type="Ламинат", collection=coll, parent=coll,
                   collection_ref="YO-C1", alt_units={},
                   purchase=p(purchase, "purchase"), retail=p(retail, "retail"),
                   rrc=p(rrc, "rrc"))


SOURCE = {("YO-1", "2026-07-20"): {"supplier": "Монарх Логистик",
                                   "price_doc": "Монарх-логистик",
                                   "price_date": "2026-07-15"}}


class DominantTest(unittest.TestCase):
    def test_majority_wins(self):
        self.assertEqual(dominant(["a"] * 15 + ["b"] * 10)[0], "a")

    def test_exactly_half_is_not_dominant(self):
        """«Преобладание» — строго больше половины, иначе выделить нечего."""
        self.assertIsNone(dominant(["a", "a", "b", "b"])[0])

    def test_no_dates(self):
        self.assertEqual(dominant([None, None]), (None, 0, 0))

    def test_counts_only_known_dates(self):
        self.assertEqual(dominant(["a", "a", None])[1:], (2, 2))


class ProductTest(unittest.TestCase):
    def test_all_prices_one_date_with_source(self):
        text = describe_product(item(purchase=1050, rrc=1550, retail=1200), SOURCE)
        self.assertIn("закуп 1 050, РРЦ 1 550, наша роз. 1 200 от 20.07.2026", text)
        self.assertIn("Прайс «Монарх-логистик» от 15.07.2026", text)

    def test_different_dates_get_own_date_each(self):
        text = describe_product(
            item(purchase=1050, rrc=1550,
                 dates={"purchase": "2026-07-20", "rrc": "2026-06-12"}), SOURCE)
        self.assertIn("закуп 1 050 от 20.07.2026", text)
        self.assertIn("РРЦ 1 550 от 12.06.2026", text)

    def test_unknown_source_admitted_not_invented(self):
        """Даты в 1С есть, а записи в журнале нет — значит правили руками, так и говорим."""
        text = describe_product(item(purchase=1050), {})
        self.assertIn("20.07.2026", text)
        self.assertIn("вручную", text)
        self.assertNotIn("Прайс «", text)

    def test_no_prices(self):
        self.assertIn("не заданы", describe_product(item(), SOURCE))


class GroupTest(unittest.TestCase):
    def test_uniform_collection_reads_like_product(self):
        items = [item(ref=f"YO-{i}", purchase=1050, rrc=1550) for i in range(5)]
        text = describe_group("Adventure", items, SOURCE)
        self.assertIn("Adventure (5 поз.)", text)
        self.assertIn("закуп 1 050, РРЦ 1 550 от 20.07.2026", text)

    def test_same_day_different_values(self):
        items = [item(ref=f"YO-{i}", purchase=1000 + i * 50) for i in range(4)]
        text = describe_group("Adventure", items, SOURCE)
        self.assertIn("цены менялись 20.07.2026", text)
        self.assertIn("значения у товаров разные", text)

    def test_dominant_date_reported_with_remainder(self):
        items = ([item(ref=f"YO-a{i}", purchase=1000) for i in range(15)]
                 + [item(ref=f"YO-b{i}", purchase=900, dates={"purchase": "2026-03-01"})
                    for i in range(10)])
        text = describe_group("Adventure", items, SOURCE)
        self.assertIn("у 15 из 25 товаров цены менялись 20.07.2026", text)
        self.assertIn("У остальных (10 поз.) — в другие даты", text)

    def test_no_dominance_sends_to_1c(self):
        items = [item(ref=f"YO-{i}", purchase=1000, dates={"purchase": f"2026-0{i+1}-01"})
                 for i in range(4)]
        text = describe_group("Adventure", items, SOURCE)
        self.assertIn("в разное время, посмотри в 1С", text)

    def test_empty_collection(self):
        self.assertIn("не заданы", describe_group("Adventure", [item()], SOURCE))


class FormatTest(unittest.TestCase):
    def test_date_format(self):
        self.assertEqual(fmt_date("2026-07-20"), "20.07.2026")
        self.assertEqual(fmt_date(None), "?")


if __name__ == "__main__":
    unittest.main()
