"""Повторная присылка ТОГО ЖЕ прайса не должна стоить заново (§6.9).

Две разные экономии, и путать их нельзя:

* **кеш разбора** — снимает CPU: файл на 12 870 строк парсился по два раза за вызов
  (в самом чтении и внутри сигнатуры) плюс отдельное открытие книги ради картинок.
  Токенов это не экономит: за них платят при передаче текста модели;
* **раскладка** — снимает токены: разведка «какой бренд в каких строках» не повторяется.

Обе чистятся по одному правилу: прайс НОВЕЕ от того же поставщика отменяет прежнее.
"""
import io as _io
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.agent.pricing_tools import PricingTools, clear_nomenclature_cache
from src.price_tool.parser import clear_parse_cache, parse_price_table
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec, item

TMS = [{"code": "T1", "name": "APE", "first_row": 12, "last_row": 140},
       {"code": "T2", "name": "Ceracasa", "first_row": 141, "last_row": 320}]


def book(rows=200, title="Прайс"):
    wb = Workbook(); ws = wb.active; ws.title = "Price"
    ws.append([title])
    ws.append(["Артикул", "Наименование", "Опт", "Розн"])
    for i in range(rows):
        ws.append([f"A{i}", f"Плитка {i}", 900 + i, 1400 + i])
    buf = _io.BytesIO(); wb.save(buf); return buf.getvalue()


class ParseCacheTest(unittest.TestCase):
    def setUp(self):
        clear_parse_cache()

    def test_second_parse_is_served_from_cache(self):
        content = book()
        first = parse_price_table(content, "p.xlsx")
        second = parse_price_table(content, "p.xlsx")
        self.assertEqual([s.name for s in first], [s.name for s in second])
        self.assertEqual(len(first[0].rows), len(second[0].rows))

    def test_cache_returns_a_copy(self):
        """Вызывающие вставляют в строки маркеры картинок — общий объект отдавать нельзя."""
        content = book()
        first = parse_price_table(content, "p.xlsx")
        first[0].rows[0][0] = "⟨ИЗОБРАЖЕНИЕ #1⟩"
        second = parse_price_table(content, "p.xlsx")
        self.assertNotEqual(second[0].rows[0][0], "⟨ИЗОБРАЖЕНИЕ #1⟩")

    def test_different_file_is_parsed_anew(self):
        a = parse_price_table(book(rows=10), "p.xlsx")
        b = parse_price_table(book(rows=20), "p.xlsx")
        self.assertNotEqual(len(a[0].rows), len(b[0].rows))


class LayoutStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_save_and_read(self):
        await self.store.save_layout("sig1", "Артисан-Проект", "Price.xls",
                                     "2026-08-26", TMS)
        got = await self.store.get_layout("sig1")
        self.assertEqual([s["name"] for s in got["sections"]], ["APE", "Ceracasa"])
        self.assertEqual(got["sections"][0]["first_row"], 12)

    async def test_same_price_keeps_layout(self):
        """Тот же прайс, присланный заново, — ровно то, ради чего кеш и заведён."""
        await self.store.save_layout("sig1", "Артисан-Проект", "Price.xls",
                                     "2026-08-26", TMS)
        await self.store.drop_old_layouts("Артисан-Проект", "2026-08-26")
        self.assertIsNotNone(await self.store.get_layout("sig1"))

    async def test_newer_price_clears_it(self):
        await self.store.save_layout("sig1", "Артисан-Проект", "Price.xls",
                                     "2026-08-26", TMS)
        self.assertEqual(await self.store.drop_old_layouts("Артисан-Проект",
                                                           "2026-09-15"), 1)
        self.assertIsNone(await self.store.get_layout("sig1"))

    async def test_older_price_and_other_supplier_do_not_clear(self):
        await self.store.save_layout("sig1", "Артисан-Проект", "Price.xls",
                                     "2026-08-26", TMS)
        self.assertEqual(await self.store.drop_old_layouts("Артисан-Проект",
                                                           "2026-07-01"), 0)
        self.assertEqual(await self.store.drop_old_layouts("Монарх", "2026-09-15"), 0)
        self.assertIsNotNone(await self.store.get_layout("sig1"))

    async def test_unknown_date_clears_nothing(self):
        await self.store.save_layout("sig1", "Артисан-Проект", "p", "2026-08-26", TMS)
        self.assertEqual(await self.store.drop_old_layouts("Артисан-Проект", None), 0)

    async def test_empty_sections_not_saved(self):
        await self.store.save_layout("sig2", "X", "p", "2026-08-26", [])
        self.assertIsNone(await self.store.get_layout("sig2"))


class LayoutThroughToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_nomenclature_cache(); clear_parse_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.content = book()
        self.tools = PricingTools(FakeOnec([item("YO-1", 949)]), self.store, user_id=42)
        self.tools.set_file("Price.xls", self.content)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _plan(self):
        return await self.tools.execute("start_price_run", {
            "supplier": "Артисан-Проект", "price_doc": "Price.xls",
            "price_date": "2026-08-26", "trademarks": TMS})

    async def test_first_read_has_no_layout(self):
        text = await self.tools.execute("read_price_file", {})
        self.assertNotIn("УЖЕ РАЗБИРАЛСЯ", text)

    async def test_layout_shown_on_repeat(self):
        await self._plan()
        fresh = PricingTools(FakeOnec([item("YO-1", 949)]), self.store, user_id=42)
        fresh.set_file("Price.xls", self.content)
        text = await fresh.execute("read_price_file", {})
        self.assertIn("УЖЕ РАЗБИРАЛСЯ", text)
        self.assertIn("APE (T1), строки 12–140", text)
        self.assertIn("Разведку брендов повторять НЕ НУЖНО", text)

    async def test_newer_price_from_same_supplier_wipes_the_old_one(self):
        """Прошлый прайс того же поставщика устаревает, текущий — остаётся."""
        await self.store.save_layout("прошлый-файл", "Артисан-Проект", "old.xls",
                                     "2026-07-01", TMS)
        await self._plan()                       # текущий файл, дата 2026-08-26
        self.assertIsNone(await self.store.get_layout("прошлый-файл"))

        fresh = PricingTools(FakeOnec([item("YO-1", 949)]), self.store, user_id=42)
        fresh.set_file("Price.xls", self.content)
        self.assertIn("УЖЕ РАЗБИРАЛСЯ", await fresh.execute("read_price_file", {}))

    async def test_resending_the_same_price_keeps_the_layout(self):
        """Главный сценарий: тот же файл прислали заново — разведка не повторяется."""
        await self._plan()
        await self._plan()                       # повторная присылка, дата та же
        fresh = PricingTools(FakeOnec([item("YO-1", 949)]), self.store, user_id=42)
        fresh.set_file("Price.xls", self.content)
        self.assertIn("APE (T1), строки 12–140",
                      await fresh.execute("read_price_file", {}))


if __name__ == "__main__":
    unittest.main()
