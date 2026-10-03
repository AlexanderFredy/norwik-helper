"""Закупка из розницы: скидка дилера и курс валюты (решение админа 03.10.2026).

У части поставщиков закупочных цен в прайсе нет вовсе — «Остатки 01.10.2026» это розница в
рублях, розница в евро, свободный остаток и наличие. Дилеру такой поставщик даёт скидку
процентом от розницы, СВОИМ у каждой марки, а валютную цену надо приводить курсом. Формула
подтверждена админом: закупка = розница × (1 − скидка/100), РРЦ = розница.

Проверяется то, что ломается дорого: ноль вместо «не задано» (1С не различает пустое число и
нуль, и считать ноль скидкой значит записать закупку, равную рознице, — потерять всю маржу),
подмена розницы закупкой и пересчёт по чужой скидке.
"""
import asyncio
import unittest
from decimal import Decimal

from src.model import price_check
from src.model.dealer_price import Terms, explain, from_retail, missing, shape
from src.model.task_builder import TaskBuilderTools
from src.price_tool.parser import Sheet


#: Валюта прайса: код по международному классификатору и её курс.
EUR = Terms(discount=10, rate=98.4321, currency="978", currency_name="EUR")


class FormulaTest(unittest.TestCase):

    def test_discount_from_retail(self):
        got = from_retail(1000, Terms(discount=17.5))
        self.assertEqual(got, {"purchase": Decimal("825.00"), "rrc": Decimal("1000.00")})

    def test_currency_goes_through_the_rate(self):
        """49,59 EUR × 98,4321 × 0,9 = 4393,12 — сверено с боевым файлом."""
        got = from_retail(49.59, EUR, currency=True)
        self.assertEqual(got["purchase"], Decimal("4393.12"))
        self.assertEqual(got["rrc"], Decimal("4881.25"))

    def test_rrc_is_the_retail_itself(self):
        """Другого источника рекомендованной цены в таком прайсе нет, а выдумывать её
        нельзя."""
        self.assertEqual(from_retail(2897, Terms(discount=30))["rrc"],
                         Decimal("2897.00"))

    def test_zero_discount_means_not_set(self):
        """В 1С `Скидка` — числовой реквизит, пустое поле приезжает нулём. Считать его
        настоящей скидкой значило бы записать закупку, равную рознице."""
        self.assertEqual(from_retail(1000, Terms(discount=0)), {})
        self.assertEqual(from_retail(1000, Terms(discount=None)), {})

    def test_nonsense_discount_is_refused(self):
        self.assertEqual(from_retail(1000, Terms(discount=150)), {})
        self.assertEqual(from_retail(1000, Terms(discount=-5)), {})

    def test_currency_without_a_rate_is_refused(self):
        self.assertEqual(from_retail(10, Terms(discount=17.5, currency="978"),
                                     currency=True), {})

    def test_rate_without_a_currency_is_refused(self):
        """Хуже отсутствия: число выглядит заданным, а чего оно курс — неизвестно, и по
        нему однажды посчитали бы цену не в той валюте."""
        self.assertEqual(from_retail(10, Terms(discount=17.5, rate=98), currency=True), {})

    def test_zero_rate_means_not_set(self):
        self.assertEqual(from_retail(10, Terms(discount=17.5, currency="978", rate=0),
                                     currency=True), {})

    def test_no_price_no_result(self):
        self.assertEqual(from_retail(None, Terms(discount=10)), {})
        self.assertEqual(from_retail(0, Terms(discount=10)), {})

    def test_missing_names_the_currency_itself(self):
        """Валюта у прайса одна и указывает её человек: не указана — говорим именно это,
        а не про курс, которого не к чему приложить."""
        text = missing(Terms(), currency=True)
        self.assertIn("скидка", text)
        self.assertIn("валюта прайса", text)
        self.assertEqual(missing(Terms(discount=10)), "")

    def test_missing_names_the_rate_of_that_currency(self):
        text = missing(Terms(discount=10, currency="840", currency_name="USD"),
                       currency=True)
        self.assertIn("курс USD", text)

    def test_explain_is_checkable_by_eye(self):
        """Закупки в прайсе не стоит, и без этой строки происхождение цифры неизвестно
        никому, включая админа через месяц."""
        text = explain(Terms(discount=17.5, rate=98.4321, currency="978",
                             currency_name="EUR"), currency=True)
        self.assertIn("17,5%", text)
        self.assertIn("98,4321", text)
        self.assertIn("EUR", text)


class ShapeTest(unittest.TestCase):

    def test_retail_only_is_recognised(self):
        rows = [["Прайс-лист"], ["Розничная цена в рублях", "Розничная цена в евро"]]
        got = shape(rows)
        self.assertTrue(got.retail_only)
        self.assertTrue(got.currency)

    def test_other_currencies_are_recognised_too(self):
        """Евро не единственный случай: бывают доллары и юани (правка админа 03.10.2026)."""
        self.assertTrue(shape([["Цена, USD"]]).currency)
        self.assertTrue(shape([["Цена CNY"]]).currency)
        self.assertFalse(shape([["Цена, руб."]]).currency)

    def test_root_is_rozn_not_roznic(self):
        """Поймано на живом файле: колонка зовётся «Розничная», и шаблон «розниц» её не
        находил — признак молча считал прайс обычным."""
        self.assertTrue(shape([["Розничная цена"]]).retail)

    def test_purchase_beats_retail(self):
        """Есть закупка — пересчитывать нечего, прайс обычный."""
        rows = [["Артикул", "Закупка", "РРЦ"]]
        self.assertFalse(shape(rows).retail_only)

    def test_dealer_and_opt_count_as_purchase(self):
        self.assertFalse(shape([["Цена опт", "РРЦ"]]).retail_only)
        self.assertFalse(shape([["Дилерская цена", "Розница"]]).retail_only)

    def test_header_only_is_scanned(self):
        """Ниже в данных «розница» попадается в названии товара, и по всему листу признак
        срабатывал бы где угодно."""
        rows = [["Артикул", "Цена"]] + [["A1", "розница магазина"]] * 30
        self.assertFalse(shape(rows, depth=1).retail)


HEAD = [["Прайс"], ["Артикул", "Бренд", "руб.", "EUR"]]


def sheet(*data):
    return Sheet(name="TDSheet", rows=[list(r) for r in HEAD] + [list(r) for r in data])


class ComparisonTest(unittest.TestCase):
    """Сверка цен по прайсу без закупки."""

    def cols(self, rows, **spec):
        got = price_check.resolve_columns(rows, spec)
        self.assertNotIsInstance(got, str, got)
        return got

    def test_old_name_of_the_currency_column_is_accepted(self):
        """`retail_eur` — прежнее имя: запомненный маппинг колонок живёт у сигнатуры и
        переживает выкладку, а валюта стала настраиваемой."""
        cols = self.cols(HEAD, article=1, retail_eur=4)
        self.assertEqual(cols.retail_cur, 3)

    def test_retail_columns_are_accepted(self):
        cols = self.cols(HEAD, article=1, retail=3)
        self.assertTrue(cols.from_retail)
        self.assertEqual(cols.retail, 2)

    def test_prices_are_computed_per_row_brand(self):
        """Скидка у каждого бренда СВОЯ, а строки лежат на одном листе."""
        book = sheet(["4938", "ABK", "1000", ""],
                     ["K9470", "VitrA", "2000", ""])
        tools = TaskBuilderTools(b"x", "x.xls", discounts={"ABK": 10, "VitrA": 30})
        cols = self.cols(book.rows, article=1, retail=3)

        got = price_check.prices_from_rows(book.rows, cols, tools._terms_reader(book))

        self.assertEqual(got["4938"]["purchase"], Decimal("900.00"))
        self.assertEqual(got["k9470"]["purchase"], Decimal("1400.00"))

    def test_without_terms_nothing_is_computed(self):
        """Отсутствие условий не имеет права дать ЦЕНУ — только её отсутствие."""
        book = sheet(["4938", "ABK", "1000", ""])
        cols = self.cols(book.rows, article=1, retail=3)
        self.assertEqual(price_check.prices_from_rows(book.rows, cols, None), {})

    def test_named_purchase_wins_over_retail(self):
        """Назвала закупку — значит в прайсе она есть, и пересчитывать нечего."""
        book = sheet(["4938", "ABK", "1000", ""])
        cols = self.cols(book.rows, article=1, purchase=3, retail=3)
        got = price_check.prices_from_rows(book.rows, cols, None)
        self.assertEqual(got["4938"]["purchase"], Decimal("1000"))

    def test_rouble_retail_is_preferred_over_currency(self):
        """Для рублёвой не нужен курс — то есть меньше того, что может быть не задано."""
        book = sheet(["4938", "ABK", "1000", "10"])
        tools = TaskBuilderTools(b"x", "x.xls", discounts={"ABK": 10})
        cols = self.cols(book.rows, article=1, retail=3, retail_cur=4)
        got = price_check.prices_from_rows(book.rows, cols, tools._terms_reader(book))
        self.assertEqual(got["4938"]["rrc"], Decimal("1000.00"))

    def test_merged_brand_cell_carries_down(self):
        book = sheet(["4938", "ABK", "1000", ""],
                     ["4940", "", "2000", ""])
        tools = TaskBuilderTools(b"x", "x.xls", discounts={"ABK": 10})
        cols = self.cols(book.rows, article=1, retail=3)
        got = price_check.prices_from_rows(book.rows, cols, tools._terms_reader(book))
        self.assertEqual(got["4940"]["purchase"], Decimal("1800.00"))

    def test_gap_names_the_brands_without_a_discount(self):
        """Иначе их позиции молча выпали бы из сверки как «цены нет в прайсе»."""
        book = sheet(["4938", "ABK", "1000", ""], ["K9470", "VitrA", "2000", ""])
        tools = TaskBuilderTools(b"x", "x.xls", discounts={"ABK": 10})
        cols = self.cols(book.rows, article=1, retail=3)
        gap = tools._terms_gap(book, cols)
        self.assertIn("VitrA", gap)
        self.assertNotIn("ABK", gap)


class WriteTest(unittest.IsolatedAsyncioTestCase):
    """Запись цен: розница превращается в закупку ДО всего остального."""

    def tools(self, terms=None):
        from src.model.executor import TaskTools

        return TaskTools(onec=None, content=b"x", filename="x.xls",
                         guard=lambda: None, terms=terms)

    def test_retail_becomes_purchase_and_rrc(self):
        inp = {"retail": 1000}
        self.assertEqual(self.tools(Terms(discount=17.5))._to_purchase(inp), "")
        self.assertEqual(inp["purchase"], 825.0)
        self.assertEqual(inp["rrc"], 1000.0)

    def test_items_are_converted_too(self):
        inp = {"items": [{"ref": "R1", "retail": 1000}, {"ref": "R2", "retail": 2000}]}
        self.tools(Terms(discount=10))._to_purchase(inp)
        self.assertEqual([row["purchase"] for row in inp["items"]], [900.0, 1800.0])

    def test_named_rrc_is_not_overwritten(self):
        """В прайсе могла стоять РРЦ отдельной колонкой — она точнее пересчитанной."""
        inp = {"retail": 1000, "rrc": 1500}
        self.tools(Terms(discount=10))._to_purchase(inp)
        self.assertEqual(inp["rrc"], 1500)

    def test_named_purchase_is_left_alone(self):
        inp = {"purchase": 700, "retail": 1000}
        self.tools(Terms(discount=10))._to_purchase(inp)
        self.assertEqual(inp["purchase"], 700)

    def test_without_terms_it_refuses_and_says_why(self):
        """Цена уезжает в боевую 1С: записать её по пустому курсу значит поставить товару
        цифру, которой нет, причём необратимо."""
        inp = {"retail": 1000}
        gap = self.tools(None)._to_purchase(inp)
        self.assertIn("НЕ записаны", gap)
        self.assertIn("скидка", gap)
        self.assertNotIn("purchase", inp)

    def test_currency_without_a_rate_refuses(self):
        inp = {"retail_cur": 10}
        gap = self.tools(Terms(discount=10, currency="978",
                               currency_name="EUR"))._to_purchase(inp)
        self.assertIn("курс EUR", gap)
        self.assertNotIn("purchase", inp)

    def test_old_currency_key_still_converts(self):
        inp = {"retail_eur": 10}
        self.assertEqual(self.tools(EUR)._to_purchase(inp), "")
        self.assertEqual(inp["purchase"], 885.89)

    def test_ordinary_price_is_untouched(self):
        inp = {"purchase": 1000, "rrc": 1500}
        self.assertEqual(self.tools(None)._to_purchase(inp), "")
        self.assertEqual(inp, {"purchase": 1000, "rrc": 1500})

    async def test_write_refuses_before_touching_1c(self):
        """Отказ стоит ПЕРЕД выгрузкой номенклатуры: без условий ходить в 1С незачем."""
        answer = await self.tools(None)._write_prices(
            {"tm_code": "000000265", "collection": "Eco Chic", "retail": 1000})
        self.assertIn("НЕ записаны", answer)


class BriefTest(unittest.TestCase):
    """Задание модели: условия пересчёта обязаны в нём стоять."""

    def brief(self, terms):
        from src.model.enums import TaskKind, TaskStatus, TaskSubject
        from src.model.executor import task_brief
        from src.model.price import Price, SupplierPrice
        from src.model.refs import Ref, TaskAddress
        from src.model.task import PriceTask

        price = Price(id=7, supplier_price=SupplierPrice(
            supplier_id=1, file_id=1, file_path="/p/a.xls", filename="Остатки.xls"))
        task = PriceTask(kind=TaskKind.CHANGE_PRICES, id=9,
                         address=TaskAddress(tm=Ref.make(code="000000265", names=["ABK"]),
                                             subject=Ref.make(names=["Eco Chic"]),
                                             subject_kind=TaskSubject.COLLECTION),
                         status=TaskStatus.TODO, description="сверить цены")
        return task_brief(price, task, terms)

    def test_terms_are_stated(self):
        text = self.brief(Terms(discount=17.5))  # рубли: валюта не указана
        self.assertIn("закупка = розница − 17,5%", text)
        self.assertIn("retail", text)

    def test_missing_terms_forbid_writing(self):
        text = self.brief(Terms())
        self.assertIn("писать нельзя", text)

    def test_ordinary_task_says_nothing_about_retail(self):
        self.assertNotIn("розниц", self.brief(None))


if __name__ == "__main__":
    unittest.main()
