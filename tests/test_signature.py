"""Сигнатура формата прайса (§6.5.2): устойчива к данным, чувствительна к структуре."""
import unittest

from src.price_tool.parser import Sheet
from src.price_tool.signature import price_signature


def sheet(rows, name="Прайс"):
    return Sheet(name=name, rows=rows)


HEADER = ["Артикул", "Наименование", "Закупка", "РРЦ"]


class SignatureTest(unittest.TestCase):
    def test_same_structure_same_signature(self):
        a = sheet([HEADER, ["56649", "Ламинат", "960", "1400"]])
        b = sheet([HEADER, ["56650", "Другой ламинат", "1180", "1700"]])
        self.assertEqual(price_signature([a]), price_signature([b]))

    def test_dates_in_title_do_not_change_signature(self):
        """«Прайс с 20.07» и «Прайс с 25.08» — один формат, маппинг терять нельзя."""
        july = sheet([["Прайс ОПТ с 20.07.2026"], HEADER])
        august = sheet([["Прайс ОПТ с 25.08.2026"], HEADER])
        self.assertEqual(price_signature([july]), price_signature([august]))

    def test_changed_headers_change_signature(self):
        a = sheet([HEADER, ["1", "x", "2", "3"]])
        b = sheet([["Артикул", "Наименование", "Цена дилера", "РРЦ"], ["1", "x", "2", "3"]])
        self.assertNotEqual(price_signature([a]), price_signature([b]))

    def test_sheet_name_matters(self):
        self.assertNotEqual(price_signature([sheet([HEADER], name="SPC LVT")]),
                            price_signature([sheet([HEADER], name="Ламинат")]))

    def test_empty_gives_empty(self):
        self.assertEqual(price_signature([]), "")
        self.assertEqual(price_signature([], ""), "")

    def test_pdf_text_fallback(self):
        """У pdf таблиц нет — сигнатура строится по тексту, иначе маппинг не запомнить."""
        july = "LINDERWOOD\nПрайс от 20.07.2026\nКоллекция Артикул Закупка РРЦ\nVN-511 949 1649"
        august = "LINDERWOOD\nПрайс от 25.08.2026\nКоллекция Артикул Закупка РРЦ\nVN-511 999 1649"
        self.assertTrue(price_signature([], july))
        self.assertEqual(price_signature([], july), price_signature([], august))

    def test_pdf_different_supplier_differs(self):
        one = "LINDERWOOD\nКоллекция Артикул Закупка РРЦ"
        two = "MOST FLOOR\nКоллекция Артикул Дилерская РРЦ"
        self.assertNotEqual(price_signature([], one), price_signature([], two))


if __name__ == "__main__":
    unittest.main()
