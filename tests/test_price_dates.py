"""Дата прайса из шапки листа и её дозаполнение (бой 08.10.2026).

«Price.xls» Артисаны жил без даты: в имени файла её нет, а шапку не читали. Без даты его
цены в выборе наименьшей между поставщиками не стареют никогда — недатированное
предложение считается свежим.
"""
import tempfile
import unittest
from pathlib import Path

from src.model.price_dates import backfill
from src.price_tool.freshness import date_from_sheets
from src.storage.sightings import SightingStore


class Sheet:
    def __init__(self, *rows):
        self.rows = [list(r) for r in rows]


class DateFromSheetsTest(unittest.TestCase):

    def test_numeric_date_in_the_header(self):
        """Шапка Артисаны: дата в пятой строке, над ней контакты."""
        sheet = Sheet(["Оптовый отдел", "", "ООО «Артисан-Проект»"], ["г. Москва"],
                      ["тел: (495) 933-50-33"], [""], ["Прайс-лист на 26.08.2026"])
        self.assertEqual(date_from_sheets([sheet]), "2026-08-26")

    def test_date_in_words(self):
        """Кераматика: «Прайс-лист на 1 октября 2026 г»."""
        self.assertEqual(date_from_sheets([Sheet(["Прайс-лист на 1 октября 2026 г"])]),
                         "2026-10-01")
        self.assertEqual(date_from_sheets([Sheet(["с 5 мая 2026"])]), "2026-05-05")

    def test_no_year_no_date(self):
        """«01.09» в шапке бывает чем угодно — от номера раздела до размера."""
        self.assertIsNone(date_from_sheets([Sheet(["Раздел 01.09", "Цена"])]))

    def test_dates_below_the_header_are_not_the_price_date(self):
        """Ниже шапки даты в ячейках — сроки поставки, а не дата прайса."""
        rows = [["Артикул", "Цена", "Поступление"]] + [["A1", "100", ""]] * 15 \
            + [["A2", "200", "15.11.2026"]]
        self.assertIsNone(date_from_sheets([Sheet(*rows)]))


class BackfillTest(unittest.IsolatedAsyncioTestCase):
    """Уже принятый прайс без даты получает её при старте — вместе со строками журнала."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "t.db"
        self.sightings = SightingStore(self.db)
        await self.sightings.init()
        await self.sightings.remember(7, "SIG", {"a65s": "Boost Natural"}, supplier="Артисан",
                                      price_date=None, prices={"a65s": {"purchase": 4284.5}})

        book = Path(self._dir.name) / "Price.xlsx"
        import openpyxl
        wb = openpyxl.Workbook()
        wb.active.append(["Прайс-лист на 26.08.2026"])
        wb.active.append(["Код", "Заводской код", "Опт"])
        wb.save(book)
        self.book = book

        test = self

        class Store:
            def __init__(self):
                self.dates = {}

            async def undated_prices(self):
                return [(8, str(test.book), "Price.xlsx", 7, "SIG")]

            async def set_price_date(self, price_id, value):
                self.dates[price_id] = value
                return True

        self.store = Store()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_the_price_and_its_offers_get_the_date(self):
        done = await backfill(self.store, self.sightings)

        self.assertEqual(done, [(8, "2026-08-26")])
        self.assertEqual(self.store.dates[8], "2026-08-26")
        offers = await self.sightings.offers(["a65s"])
        self.assertEqual(offers["a65s"][0].price_date, "2026-08-26",
                         "без даты предложение не стареет никогда")


if __name__ == "__main__":
    unittest.main()
