"""Юнит-тесты структурного парсера прайса (без сети)."""
import io
import unittest

from openpyxl import Workbook

from src.onec.client import (_prices_to_dict, _price, _alt_units_to_dict,
                             _parent_name, _parent_code)
from src.price_tool.parser import parse_price_table, non_empty_rows, render_preview


def _xlsx(rows: list[list], title: str = "Прайс") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = title
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class ParsePriceTableTest(unittest.TestCase):
    def test_xlsx_rows_and_number_cells(self):
        content = _xlsx([
            ["Артикул", "Наименование", "Размер", "Закупка"],
            ["56649", "Classen VisioGrande", "604x280x8", 960],   # int → "960", не "960.0"
            [None, None, None, None],                              # пустая строка
            ["56023", "Classen Beton", "604x280x8", 1140.75],
        ])
        sheets = parse_price_table(content, "p.xlsx")
        self.assertEqual(len(sheets), 1)
        self.assertEqual(sheets[0].name, "Прайс")
        rows = non_empty_rows(sheets[0])
        self.assertEqual(len(rows), 3)                            # пустая отфильтрована
        self.assertEqual(rows[1], ["56649", "Classen VisioGrande", "604x280x8", "960"])
        self.assertEqual(rows[2][3], "1140.75")

    def test_render_preview_caps_rows(self):
        data = [["Артикул", "Цена"]] + [[str(i), str(i * 10)] for i in range(300)]
        sheets = parse_price_table(_xlsx(data), "p.xlsx")
        preview = render_preview(sheets[0], max_rows=50)
        self.assertIn("показаны первые 50", preview)

    def test_unsupported_returns_empty(self):
        self.assertEqual(parse_price_table(b"whatever", "p.doc"), [])

    def test_trailing_empty_cells_trimmed(self):
        # "широкая" строка: данные + сотни пустых хвостовых колонок
        content = _xlsx([
            ["Арт", "Цена"] + [""] * 300,
            ["56649", "960"] + [""] * 300,
        ])
        rows = non_empty_rows(parse_price_table(content, "p.xlsx")[0])
        self.assertEqual(rows[0], ["Арт", "Цена"])          # хвост обрезан
        self.assertEqual(rows[1], ["56649", "960"])
        self.assertNotIn("\t\t\t", render_preview(parse_price_table(content, "p.xlsx")[0]))


class OnecPriceNormalizationTest(unittest.TestCase):
    def test_prices_array_to_dict(self):
        arr = [{"purchase": {"value": 960, "date": "2026-07-14"}},
               {"rrc": {"value": 1140.75, "date": "2026-06-07"}}]
        d = _prices_to_dict(arr)
        self.assertIn("purchase", d)
        self.assertIn("rrc", d)
        self.assertEqual(_price(d["purchase"]).value, 960.0)
        self.assertEqual(_price(d["rrc"]).date, "2026-06-07")

    def test_missing_price_is_none(self):
        self.assertIsNone(_price({}))
        self.assertIsNone(_price(None))

    def test_parent_object_gives_name_and_code(self):
        parent = {"code": "YO-00075139", "name": "Excellent"}
        self.assertEqual(_parent_name(parent), "Excellent")
        self.assertEqual(_parent_code(parent), "YO-00075139")

    def test_parent_legacy_string_still_parsed(self):
        # ранняя версия сервиса отдавала parent строкой — код папки тогда неизвестен
        self.assertEqual(_parent_name("Excellent"), "Excellent")
        self.assertEqual(_parent_code("Excellent"), "")
        self.assertEqual(_parent_name(None), "")

    def test_alt_units_array_to_dict(self):
        d = _alt_units_to_dict([{"упак": 2.367}])
        self.assertEqual(d, {"упак": 2.367})
        self.assertEqual(_alt_units_to_dict([]), {})
        self.assertEqual(_alt_units_to_dict([{"упак": "нечисло"}]), {})  # мусор пропущен


if __name__ == "__main__":
    unittest.main()
