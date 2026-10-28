"""Код Артисаны «+…» → реквизит «Арт» (решение админа 28.10.2026)."""
import unittest
from io import BytesIO

from src.model.supplier_codes import fill_art, plus_codes
from src.onec.client import NomItem
from src.price_tool.items import plan_collection
from src.price_tool.parser import parse_price_table


def artisana(rows=60, plus=True) -> list:
    import openpyxl
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "Price"
    sheet.append(["Оптовый отдел"])
    sheet.append(["Код", "Заводской код", "Вид", "Размер", "Наименование", "Розн", "Опт"])
    sheet.append(["APE"])                                   # раздел бренда
    for n in range(rows):
        code = f"+{30000 + n}" if plus else f"q{30000 + n}"
        sheet.append([code, f"A{n:03d}", "Плитка", "60x120", f"Декор {n}", 1500, 1000])
    sheet.append(["q25480", "", "Плитка", "60x120", "Kerama", 1500, 1000])
    buf = BytesIO()
    wb.save(buf)
    return parse_price_table(buf.getvalue(), "Price.xlsx")


def card(art=None, article="A001"):
    return NomItem(ref="R1", id="", name="old", article=article, unit="м2", size="1200x600",
                   product_type="Керамогранит", collection="Mark", parent="Mark",
                   collection_ref="F", alt_units={}, purchase=None, retail=None, rrc=None,
                   site_name="Декор 1", product_type_ref="000000011",
                   length_from=1200, length_to=1200, width_from=600, width_to=600, art=art)


class PlusCodesTest(unittest.TestCase):

    def test_factory_article_maps_to_the_plus_code(self):
        codes = plus_codes(artisana())
        self.assertEqual(codes["a001"], "+30001")
        self.assertNotIn("", codes)

    def test_q_codes_and_other_prices_are_ignored(self):
        """Коды на «q» — не внутренний код Артисаны; прайс без «+» правило не трогает."""
        self.assertEqual(plus_codes(artisana(plus=False)), {})

    def test_fill_takes_the_article_from_the_card_when_the_edit_has_none(self):
        items = [{"op": "update", "ref": "R1", "title": "Декор 1"}]
        self.assertEqual(fill_art(items, plus_codes(artisana()), {"R1": card()}), 1)
        self.assertEqual(items[0]["art"], "+30001")


class PlanArtTest(unittest.TestCase):

    def plan(self, current):
        return plan_collection({
            "tm_code": "000000115", "tm_name": "Atlas Concorde", "collection": "Mark",
            "product_type": "000000011", "product_type_name": "Керамогранит",
            "items": [{"op": "update", "ref": "R1", "title": "Декор 1", "tail": "60x120",
                       "art": "+30001"}]}, [current], [])

    def test_new_art_is_written(self):
        ops = self.plan(card(art="")).ops()
        self.assertTrue(any(op.get("art") == "+30001" for op in ops))

    def test_same_art_is_not_rewritten(self):
        ops = self.plan(card(art="+30001")).ops()
        self.assertFalse(any("art" in op for op in ops))


if __name__ == "__main__":
    unittest.main()
