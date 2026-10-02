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


class SkeletonTakesOnlyHeadersTest(unittest.TestCase):
    """Скелет — имена листов и СТРОКА ЗАГОЛОВКОВ, данные в него попадать не должны.

    Воспроизводит бой 02.10.2026 на прайсе «Стройиндустрия (Лиля)». Раньше в скелет шли
    первые восемь непохожих на числовые строк, и туда попадали блок контактов, пометки
    брендов и первые строки ТОВАРОВ — у них в ячейках буквы. Между двумя прайсами одного
    формата поставщик убрал телефон с почтой, снял пометку «новинка» у двух позиций и
    дописал колонку цены к бренду — хеш разошёлся, формат не опознался, агент завёл второго
    поставщика с именем из имени файла, а выбор листов обнулился.
    """

    REAL = ["Замок", "Размер мм", "Класс", "Фаска", "м2 в уп", "опт/м2", "РРЦ/м2"]

    def april(self):
        return sheet([
            ['ООО "Стройиндустрия"'],
            ["+7-900-000-00-00 Лилия"],
            ["liliya-g@mail.ru"],
            ["Прайс-лист"],
            self.REAL,
            ["CLASSEN"],
            ["ULTRAFLOOR"],
            ["1 Ламинат POOL PROMO 33 WR Дуб песочный 1,9м2", "склад", "новинка"],
            ["1 Ламинат POOL PROMO 33 WR Дуб медовый 1,9м2", "склад", "новинка"],
        ], name="Прайс от 20.04.2026")

    def october(self):
        return sheet([
            ['ООО "Стройиндустрия"'],
            ["Прайс-лист"],
            self.REAL,
            ["CLASSEN"],
            ["ULTRAFLOOR", "цена 2025", "цена 2026"],
            ["1 Ламинат POOL PROMO 33 WR Дуб песочный 1,9м2", "склад"],
            ["1 Ламинат POOL PROMO 33 WR Дуб медовый 1,9м2", "склад"],
        ], name="Прайс от 01.10.2026")

    def test_contacts_markers_and_goods_do_not_change_signature(self):
        self.assertEqual(price_signature([self.april()]),
                         price_signature([self.october()]))

    def test_changed_column_headers_still_change_signature(self):
        """Чувствительность к СТРУКТУРЕ остаётся: иначе два формата слились бы в один."""
        other = sheet([['ООО "Стройиндустрия"'], ["Прайс-лист"],
                       ["Замок", "Размер мм", "Класс", "опт/уп", "РРЦ/уп"]],
                      name="Прайс от 01.10.2026")
        self.assertNotEqual(price_signature([self.october()]), price_signature([other]))

    def test_single_cell_rows_alone_give_no_header(self):
        """Лист без заголовка колонок опознаётся ИМЕНЕМ, а не выдуманной шапкой."""
        a = sheet([["ИЗМЕНЕНИЯ"], ["снято с производства"]], name="Изменения")
        b = sheet([["ИЗМЕНЕНИЯ"], ["добавлены новинки"]], name="Изменения")
        self.assertEqual(price_signature([a]), price_signature([b]))


if __name__ == "__main__":
    unittest.main()
