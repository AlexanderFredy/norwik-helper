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

    def __init__(self, article, purchase=None, rrc=None, ref="", site_name=""):
        self.article = article
        self.purchase = Price(purchase) if purchase is not None else None
        self.rrc = Price(rrc) if rrc is not None else None
        # Код 1С и расцветка — второй ключ сверки: цена на коллекцию кладётся по коду,
        # потому что артикула у позиции может не быть вовсе.
        self.ref = ref
        self.site_name = site_name


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


class FlatPriceWithoutArticlesTest(unittest.TestCase):
    """СЛУЧАЙ С БОЯ (28.09.2026). У Classen вся коллекция «Adventure WR» заведена с ПУСТЫМ
    артикулом — номер стоит в наименовании. Пять карточек из девяти держали 1098/1280 при
    прайсовой 1795/2510, а сверка молчала: и раскладка цены, и сравнение ключевались
    артикулом, которого нет.

    Цена на коллекцию потому и «на коллекцию», что относится к КАЖДОЙ её позиции.
    """

    def items(self):
        return [Item("", 1795, 2510, ref="YO-74816", site_name="Дуб Авола"),
                Item("", 1098, 1280, ref="YO-74818", site_name="Дуб Андерсон"),
                Item("", 1098, 1280, ref="YO-74819", site_name="Дуб Донкастер")]

    def test_collection_price_reaches_items_without_articles(self):
        items = self.items()
        wanted = pc.flat_prices(items, purchase=1795, rrc=2510)
        diff = pc.compare(items, wanted)

        self.assertEqual(len(diff.changed), 2, "две позиции держат чужую цену")
        self.assertEqual(diff.same, 1)
        self.assertEqual(diff.no_price_in_file, 0, "цена на коллекцию есть у всех")

    def test_position_without_an_article_is_named_by_code_and_colour(self):
        """«: закупка 1098 → 1795» без имени админ не прочтёт."""
        items = self.items()
        diff = pc.compare(items, pc.flat_prices(items, purchase=1795))
        self.assertIn("YO-74818 Дуб Андерсон", diff.changed[0])

    def test_article_still_wins_when_it_is_there(self):
        """Артикул точнее кода: колоночный разбор кладёт цены по нему, и подменять его
        кодом нельзя."""
        items = [Item("3309", 1000, ref="R1")]
        diff = pc.compare(items, {"3309": {"purchase": Decimal("2000")},
                                  "R1": {"purchase": Decimal("9999")}})
        self.assertIn("1 000 → 2 000", diff.changed[0])


#: Шапка Кераматики, как она лежит в файле: в четыре строки, цены — объединёнными ячейками.
KERAMATIKA = [
    ["Прайс-лист на 1 октября 2026 г"],
    [],
    ["Ценовая группа", "", "", "", "Розничная цена в рублях", "Розничная цена в евро",
     "Склад ответственного хранения"],
    ["Фабрика", "Бренд", "Коллекция", "Артикул", "руб.", "EUR"],
    ["", "", "", "", "Включает НДС", "Включает НДС"],
    ["", "", "", "", "Цена", "Цена", "Свободный остаток"],
    ["ABK", "ABK", "Eco Chic", "4938", "", "49.59", "13.44"],
    ["ABK", "ABK", "Eco Chic", "4940", "", "49.59", "10.8"],
]


class GuessColumnsTest(unittest.TestCase):
    """Колонки по шапке — когда модель их не назвала (бой 08.10.2026: 46 сверок Кераматики
    без единой задачи по ценам)."""

    def test_retail_only_price_list(self):
        self.assertEqual(pc.guess_columns(KERAMATIKA),
                         {"article": 4, "retail": 5, "retail_cur": 6})

    def test_the_guess_is_resolved_like_a_named_one(self):
        cols = pc.resolve_columns(KERAMATIKA, pc.guess_columns(KERAMATIKA))
        self.assertTrue(cols.from_retail)
        self.assertEqual(cols.retail_cur, 5)

    def test_purchase_and_rrc(self):
        rows = [["Артикул", "Наименование", "Цена опт", "РРЦ"],
                ["A1", "Дуб", "1000", "1500", "1"]]
        self.assertEqual(pc.guess_columns(rows), {"article": 1, "purchase": 3, "rrc": 4})

    def test_purchase_wins_over_retail(self):
        """Есть закупка — розница не нужна: сверяем то, что поставщик назвал закупкой."""
        rows = [["Артикул", "Закупка", "Розница"], ["A1", "1000", "1500", "9"]]
        self.assertEqual(pc.guess_columns(rows), {"article": 1, "purchase": 2})

    def test_two_candidates_mean_none(self):
        """«Самовывоз» и «с доставкой» у Линдервуда — выбор человека, не шаблона."""
        rows = [["Артикул", "Опт самовывоз", "Опт с доставкой"], ["A1", "1000", "1100", "1"]]
        self.assertIsNone(pc.guess_columns(rows))

    def test_rrc_alone_is_left_to_the_model(self):
        """Сверять ли РРЦ как есть или считать из неё закупку по скидке — решение."""
        rows = [["Артикул", "Наименование", "РРЦ"], ["A1", "Дуб", "1500", "1", "2"]]
        self.assertIsNone(pc.guess_columns(rows))

    def test_article_must_match_exactly(self):
        """«Код производителя» и «Артикул поставщика» рядом: по вхождению колонка уехала бы
        не туда."""
        rows = [["Артикул поставщика", "Код", "Цена опт"], ["A1", "K1", "1000", "1", "2"]]
        self.assertIsNone(pc.guess_columns(rows))

    def test_no_header_no_guess(self):
        self.assertIsNone(pc.guess_columns([["A1", "100", "200", "300"]]))
