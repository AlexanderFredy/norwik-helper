"""Сверка цен прайса с 1С при сборке задач (`src/model/price_check.py`).

СЛУЧАЙ С БОЯ (21.09.2026): по Most Flooring / Millenium Pro агент завёл задачу «обновить
цены по 8 позициям», хотя закупка и РРЦ в 1С уже совпадали с прайсом. Задача заводилась по
факту наличия ценовых колонок, сравнения не было вовсе. Здесь проверяется, что сравнение
есть и что оно молчит, когда менять нечего.
"""
import unittest
from decimal import Decimal

from src.model import price_check as pc


class Price:
    def __init__(self, value):
        self.value = value
        self.date = None


class Item:
    """Урезанная запись выгрузки: сверке нужны только артикул и две цены."""

    def __init__(self, article, purchase=None, rrc=None):
        self.article = article
        self.purchase = Price(purchase) if purchase is not None else None
        self.rrc = Price(rrc) if rrc is not None else None


ROWS = [
    ["Прайс ООО «Поставщик»", "", "", ""],
    ["Артикул", "Наименование", "Дилерская, м²", "РРЦ, м²"],
    ["3309", "Дуб Авила", "1 560,00", "2 370"],
    ["3310", "Дуб Прато", "1560", "2370"],
]


class NumberTest(unittest.TestCase):

    def test_space_and_comma_are_not_an_obstacle(self):
        self.assertEqual(pc.to_decimal("1 560,00"), Decimal("1560.00"))
        self.assertEqual(pc.to_decimal("2 370 ₽/м²"), Decimal("2370"))
        self.assertEqual(pc.to_decimal(1560), Decimal("1560"))

    def test_thousands_with_dots(self):
        self.assertEqual(pc.to_decimal("1.560.00"), Decimal("1560.00"))

    def test_nothing_is_nothing(self):
        for cell in (None, "", "—", "цена по запросу", 0):
            self.assertIsNone(pc.to_decimal(cell), cell)


class ColumnsTest(unittest.TestCase):

    def test_by_header(self):
        cols = pc.resolve_columns(ROWS, {"article": "Артикул",
                                         "purchase": "Дилерская", "rrc": "РРЦ"})
        self.assertEqual((cols.article, cols.purchase, cols.rrc), (0, 2, 3))

    def test_by_number_counted_from_one(self):
        """Модель видит лист табами, без буквенных имён колонок, и считает с единицы."""
        cols = pc.resolve_columns(ROWS, {"article": 1, "purchase": 3, "rrc": 4})
        self.assertEqual((cols.article, cols.purchase, cols.rrc), (0, 2, 3))

    def test_header_is_searched_below_the_supplier_cap(self):
        """Первая строка листа — шапка поставщика, а не заголовки."""
        cols = pc.resolve_columns(ROWS, {"article": "Артикул", "rrc": "РРЦ"})
        self.assertEqual(cols.article, 0)

    def test_unknown_column_is_an_explained_refusal(self):
        out = pc.resolve_columns(ROWS, {"article": "Артикул", "purchase": "Оптовая"})
        self.assertIsInstance(out, str)
        self.assertIn("Оптовая", out)

    def test_article_is_required(self):
        self.assertIsInstance(pc.resolve_columns(ROWS, {"purchase": 3}), str)

    def test_prices_alone_are_required_too(self):
        self.assertIsInstance(pc.resolve_columns(ROWS, {"article": 1}), str)


class ExtractTest(unittest.TestCase):

    def setUp(self):
        self.cols = pc.resolve_columns(ROWS, {"article": "Артикул",
                                              "purchase": "Дилерская", "rrc": "РРЦ"})

    def test_prices_are_taken_by_article(self):
        got = pc.prices_from_rows(ROWS, self.cols)
        self.assertEqual(got["3309"]["purchase"], Decimal("1560.00"))
        self.assertEqual(got["3309"]["rrc"], Decimal("2370"))

    def test_header_row_is_not_a_price(self):
        self.assertNotIn(pc.__name__, pc.prices_from_rows(ROWS, self.cols))
        self.assertEqual(set(pc.prices_from_rows(ROWS, self.cols)), {"3309", "3310"})

    def test_first_row_wins(self):
        """Ниже по листу тот же артикул повторяется в блоке «в упаковке»."""
        rows = ROWS + [["3309", "Дуб Авила, упак", "3 693", "5 610"]]
        self.assertEqual(pc.prices_from_rows(rows, self.cols)["3309"]["purchase"],
                         Decimal("1560.00"))


class CompareTest(unittest.TestCase):

    def setUp(self):
        cols = pc.resolve_columns(ROWS, {"article": "Артикул",
                                         "purchase": "Дилерская", "rrc": "РРЦ"})
        self.from_price = pc.prices_from_rows(ROWS, cols)

    def test_matching_prices_produce_nothing(self):
        """Ровно случай с боя: цены уже такие — задачи быть не должно."""
        diff = pc.compare([Item("3309", 1560, 2370), Item("3310", 1560, 2370)],
                          self.from_price)
        self.assertEqual(diff.changed, [])
        self.assertEqual(diff.same, 2)
        self.assertFalse(diff.anything)
        self.assertIn("НЕ заводи", pc.report(diff)["вывод"])

    def test_small_difference_is_within_the_threshold(self):
        """Порог общий с записью цен (`SAME_PRICE_PCT`), своего тут нет."""
        diff = pc.compare([Item("3309", 1555, 2370)], self.from_price)
        self.assertEqual(diff.changed, [])

    def test_real_difference_is_named_with_numbers(self):
        diff = pc.compare([Item("3309", 1200, 2370)], self.from_price)
        self.assertEqual(len(diff.changed), 1)
        self.assertIn("3309", diff.changed[0])
        self.assertIn("1 200", diff.changed[0])
        self.assertIn("1 560", diff.changed[0])
        self.assertIn("закупка", diff.changed[0])

    def test_missing_price_in_1c_is_a_difference(self):
        """Пустая цена — это «писать придётся», а не «совпало»."""
        diff = pc.compare([Item("3309", None, 2370)], self.from_price)
        self.assertEqual(len(diff.changed), 1)
        self.assertIn("не заполнена", diff.changed[0])

    def test_item_absent_from_the_file_is_counted_separately(self):
        """Позиция 1С, которой в прайсе нет, — не расхождение цен: её судьбу решает
        задача «перенос в снятые», а не ценовая."""
        diff = pc.compare([Item("9999", 100, 200)], self.from_price)
        self.assertEqual(diff.changed, [])
        self.assertEqual(diff.no_price_in_file, 1)
        self.assertEqual(pc.report(diff)["без_цены_в_прайсе"], 1)

    def test_report_counts_matches_instead_of_listing_them(self):
        """Перечень совпавших позиций агенту не нужен — это токены в каждом запросе."""
        diff = pc.compare([Item("3309", 1560, 2370), Item("3310", 1560, 2370)],
                          self.from_price)
        self.assertEqual(pc.report(diff)["совпадают"], 2)
        self.assertEqual(pc.report(diff)["расходятся"], [])


if __name__ == "__main__":
    unittest.main()
