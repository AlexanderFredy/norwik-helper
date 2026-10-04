"""Бренды внутри одного листа: колонка, состав, фильтр строк (решение админа 03.10.2026).

Выбор листов не помогает прайсу, у которого лист один: «Остатки 01.10.2026» — одна вкладка,
1286 строк, 23 бренда, 88 тыс. токенов за полный разбор, и 13 брендов из 23 в справочнике 1С
отсутствуют вовсе. Фильтровать надо СТРОКИ, а признак стоит в колонке «Бренд».

Форма шапки здесь повторяет боевой файл: четыре строки заголовков вперемешку с уточнениями,
«Фабрика» слева от «Бренда» и «Код производителя» справа — ловушки, на которых ошибочное
правило и ломается.
"""
import unittest

from src.price_tool.brands import (BrandColumn, brands_in, find_brand_column,
                                   only_brands)
from src.price_tool.parser import Sheet

HEAD = [
    ["Прайс-лист на 1 октября 2026 г."],
    ["Ценовая группа", "", "", "Розничная цена в рублях"],
    ["Фабрика", "Бренд", "Коллекция", "Артикул", "Код производителя", "Номенклатура"],
    ["", "", "", "", "", "шт. в кор."],
]


def sheet(*data, head=None):
    return Sheet(name="TDSheet", rows=[list(r) for r in (head or HEAD)] + [list(r) for r in data])


FILE = sheet(
    ["ABK", "ABK", "Eco Chic", "4938", "4938", "Керамогранит Almond"],
    ["ABK", "ABK", "Play", "0003477", "0003477", "Керамогранит Sky"],
    ["Италон", "Italon Керамический гранит", "Charme", "610010", "610010", "Гранит Extra"],
    ["VitrA", "VitrA", "Softcement", "K9470", "K9470", "Плитка Grey"],
)


class ColumnTest(unittest.TestCase):

    def test_column_and_header_height(self):
        spot = find_brand_column(FILE)
        self.assertEqual((spot.column, spot.header_rows), (1, 4))

    def test_factory_does_not_win(self):
        """«Фабрика» — ЗАВОД, а не бренд. На смешении завода с маркой мы уже обожглись у
        Peli; попади она сюда, фильтр резал бы прайс по изготовителю."""
        spot = find_brand_column(FILE)
        self.assertEqual(FILE.rows[2][spot.column], "Бренд")

    def test_manufacturer_code_does_not_win(self):
        """Сравнение ТОЧНОЕ: вхождением «Код производителя» совпал бы с «производитель» и
        увёл бы колонку на артикул."""
        head = [["Фабрика", "Код производителя", "Производитель", "Номенклатура"]]
        got = find_brand_column(sheet(["З", "4938", "ABK", "Плитка"], head=head))
        self.assertEqual(got.column, 2)

    def test_no_brand_column_at_all(self):
        head = [["Артикул", "Наименование", "Цена"]]
        self.assertIsNone(find_brand_column(sheet(["A1", "Плитка", "100"], head=head)))

    def test_header_without_data_is_not_a_column(self):
        """Заголовок есть, товаров под ним нет — фильтровать нечего."""
        self.assertIsNone(find_brand_column(sheet(head=HEAD)))

    def test_header_below_the_scan_depth_is_not_a_header(self):
        filler = [[f"строка {n}"] for n in range(25)]
        self.assertIsNone(find_brand_column(
            sheet(["ABK", "ABK", "Eco", "1", "1", "Плитка"], head=filler)))


class BrandsTest(unittest.TestCase):

    def test_order_is_the_file_order_with_counts(self):
        """Не алфавитный намеренно: админ ищет бренд там, где он стоит в книге."""
        self.assertEqual(brands_in(FILE, find_brand_column(FILE)),
                         [("ABK", 2), ("Italon Керамический гранит", 1), ("VitrA", 1)])

    def test_merged_cells_carry_the_brand_down(self):
        """Пустая ячейка — объединённая с верхней. Иначе бренд рассыпается на куски:
        первая строка досталась бы ему, остальные — никому."""
        book = sheet(
            ["ABK", "ABK", "Eco Chic", "4938", "4938", "Almond"],
            ["", "", "Eco Chic", "4940", "4940", "Avana"],
            ["", "", "Play", "0003477", "0003477", "Sky"],
            ["VitrA", "VitrA", "Soft", "K9470", "K9470", "Grey"],
        )
        self.assertEqual(brands_in(book, find_brand_column(book)),
                         [("ABK", 3), ("VitrA", 1)])

    def test_name_is_kept_as_written(self):
        """Сравнивать будем нормализованное, а показывать — авторское: админ узнаёт ярлык
        таким, каким он стоит в файле."""
        names = [name for name, _ in brands_in(FILE, find_brand_column(FILE))]
        self.assertIn("Italon Керамический гранит", names)


class FilterTest(unittest.TestCase):

    def spot(self):
        return find_brand_column(FILE)

    def test_header_survives_the_filter(self):
        """Отдав модели строки товаров без шапки, мы отдали бы таблицу без названий
        колонок — а по этому формату их четыре строки."""
        got = only_brands(FILE, self.spot(), ["ABK"])
        self.assertEqual(got.rows[:4], HEAD)

    def test_only_wanted_rows_remain(self):
        got = only_brands(FILE, self.spot(), ["ABK"])
        self.assertEqual([r[5] for r in got.rows[4:]],
                         ["Керамогранит Almond", "Керамогранит Sky"])

    def test_case_and_spaces_do_not_matter(self):
        got = only_brands(FILE, self.spot(), ["  vitra  "])
        self.assertEqual([r[5] for r in got.rows[4:]], ["Плитка Grey"])

    def test_several_brands_keep_the_file_order(self):
        got = only_brands(FILE, self.spot(), ["VitrA", "ABK"])
        self.assertEqual([r[1] for r in got.rows[4:]], ["ABK", "ABK", "VitrA"])

    def test_nothing_wanted_gives_the_header_alone(self):
        """Законный исход «ни один бренд не отмечен» — разбирает его вызывающий, а не мы
        молча: ноль задач в этой модели означает «расхождений нет»."""
        got = only_brands(FILE, self.spot(), [])
        self.assertEqual(got.rows, HEAD)

    def test_unknown_brand_gives_the_header_alone(self):
        got = only_brands(FILE, self.spot(), ["Kronotex"])
        self.assertEqual(got.rows, HEAD)

    def test_merged_rows_are_filtered_by_the_carried_brand(self):
        book = sheet(
            ["ABK", "ABK", "Eco Chic", "4938", "4938", "Almond"],
            ["", "", "Eco Chic", "4940", "4940", "Avana"],
            ["VitrA", "VitrA", "Soft", "K9470", "K9470", "Grey"],
        )
        got = only_brands(book, find_brand_column(book), ["ABK"])
        self.assertEqual([r[5] for r in got.rows[4:]], ["Almond", "Avana"])

    def test_filtered_sheet_keeps_the_name(self):
        """Имя листа едет в ответ инструмента и в `read_sheets`: подменив его, мы соврали
        бы агенту о том, что он читает."""
        self.assertEqual(only_brands(FILE, self.spot(), ["ABK"]).name, "TDSheet")

class RepeatedHeaderTest(unittest.TestCase):
    """ПОВТОРЁННАЯ ШАПКА — НЕ БРЕНД (поймано на боевом файле FLOOR SERVICE 04.10.2026).

    Поставщик размечает разделы листа, повторяя строку заголовков («АКЦИИ», «ПОДЛОЖКА И
    ПЛИНТУС»), и слово «Производитель» приезжало в список брендов наравне с Kronotex: админ
    видел в форме бренд, которого не существует, а отметив его — отфильтровал бы лист по
    строкам-разделителям. Отличить можно ровно так: это и есть одно из тех слов, по которым
    колонка была найдена.
    """

    SHEET = sheet(
        ["Kronotex", "Kronotex", "Exquisit", "4786", "4786", "Ламинат Дуб"],
        ["", "", "Dynamic", "1234", "1234", "Ламинат Клён"],
        ["Производитель", "Производитель", "Коллекция", "Артикул", "Код", "Номенклатура"],
        ["Kronopol", "Kronopol", "Aurum", "5550", "5550", "Ламинат Ясень"],
    )

    def setUp(self):
        self.spot = find_brand_column(self.SHEET)

    def test_header_word_is_not_listed_as_a_brand(self):
        self.assertEqual([b for b, _ in brands_in(self.SHEET, self.spot)],
                         ["Kronotex", "Kronopol"])

    def test_the_header_row_does_not_inherit_the_brand_above(self):
        """Строка-разделитель не принадлежит ни одному бренду: приписав её верхнему, мы
        отдали бы модели заголовок как товар Kronotex."""
        kept = only_brands(self.SHEET, self.spot, ["Kronotex"]).rows
        self.assertEqual([r[2] for r in kept[self.spot.header_rows:]],
                         ["Exquisit", "Dynamic"])

    def test_rows_after_the_repeat_belong_to_their_own_brand(self):
        kept = only_brands(self.SHEET, self.spot, ["Kronopol"]).rows
        self.assertEqual([r[2] for r in kept[self.spot.header_rows:]], ["Aurum"])

    def test_counts_do_not_include_the_repeat(self):
        self.assertEqual(dict(brands_in(self.SHEET, self.spot)),
                         {"Kronotex": 2, "Kronopol": 1})

if __name__ == "__main__":
    unittest.main()
