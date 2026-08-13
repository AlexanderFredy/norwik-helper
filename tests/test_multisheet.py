"""Мультилистовой прайс: маппинг по листам и защиты предложения.

Регрессия из боевого прогона (прайс Монарха, 4 листа): маппинг хранился один на файл, и
запомнив лист «SPC LVT», агент сузил до него всю работу — лист ламината вместе с ТМ AGT
молча выпал из предложения.
"""
import io as _io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.agent.pricing_tools import PricingTools, clear_nomenclature_cache
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec, item


def book(*sheets: str) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name in sheets:
        ws = wb.create_sheet(title=name)
        ws.append([f"Прайс, раздел {name}"])
        ws.append(["Артикул", "Наименование", "самовывоз", "с доставкой", "РРЦ"])
        ws.append([f"{name}-1", f"Товар {name}", 949, 999, 1649])
    buf = _io.BytesIO(); wb.save(buf); return buf.getvalue()


class MultiSheetMappingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "t.db"
        self.store = PricingStore(self.db)
        await self.store.init()
        self.onec = FakeOnec([item("YO-1", 949, 1649, 1139)])
        self.tools = PricingTools(self.onec, self.store, user_id=42)
        self.tools.set_file("monarh.xlsx", book("SPC LVT", "LA", "Плинтус"))

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_sheets_do_not_evict_each_other(self):
        """Ровно тот баг: сохранение второго листа затирало первый."""
        await self.tools.execute("save_price_mapping", {
            "sheet": "SPC LVT", "purchase_column": "Цена за м2 (от пачки)"})
        await self.tools.execute("save_price_mapping", {
            "sheet": "LA", "purchase_column": "цена за м² (предоплата)"})

        text = await self.tools.execute("read_price_file", {})
        self.assertIn("лист «SPC LVT»", text)
        self.assertIn("лист «LA»", text)
        self.assertIn("Цена за м2 (от пачки)", text)
        self.assertIn("цена за м² (предоплата)", text)

    async def test_remembered_sheet_does_not_cancel_the_others(self):
        """Блок маппинга обязан явно требовать разбора остальных листов."""
        await self.tools.execute("save_price_mapping", {
            "sheet": "SPC LVT", "purchase_column": "Цена за м2 (от пачки)"})
        text = await self.tools.execute("read_price_file", {})
        self.assertIn("ОСТАЛЬНЫЕ листы", text)
        self.assertIn("Запомнены только листы: SPC LVT", text)

    async def test_same_sheet_is_overwritten(self):
        await self.tools.execute("save_price_mapping", {
            "sheet": "LA", "purchase_column": "самовывоз"})
        await self.tools.execute("save_price_mapping", {
            "sheet": "LA", "purchase_column": "с доставкой"})
        rows = await self.store.list_mappings()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mapping"]["purchase_column"], "с доставкой")

    async def test_forget_one_sheet_keeps_others(self):
        await self.tools.execute("save_price_mapping", {
            "sheet": "SPC LVT", "purchase_column": "а"})
        await self.tools.execute("save_price_mapping", {"sheet": "LA", "purchase_column": "б"})
        rows = await self.store.list_mappings()
        sig = rows[0]["signature"]
        self.assertTrue(await self.store.forget_mapping(sig, "LA"))
        left = await self.store.get_mappings(sig)
        self.assertEqual([m["sheet"] for m in left], ["SPC LVT"])


class MappingMigrationTest(unittest.IsolatedAsyncioTestCase):
    """Боевая база уже жила со старым ключом — переезд не должен терять трактовки."""

    async def test_sheet_moves_from_json_to_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE price_mappings (signature TEXT PRIMARY KEY, "
                       "supplier TEXT, mapping TEXT NOT NULL, updated_at TEXT NOT NULL, "
                       "uses INTEGER NOT NULL DEFAULT 0)")
            db.execute("INSERT INTO price_mappings VALUES (?, ?, ?, ?, ?)",
                       ("e3cc693dd38c6e06", "Монарх Логистик",
                        json.dumps({"sheet": "SPC LVT", "purchase_column": "Цена за м2"},
                                   ensure_ascii=False), "2026-08-12T13:15:00", 2))
            db.commit(); db.close()

            store = PricingStore(path)
            await store.init()
            rows = await store.list_mappings()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sheet"], "SPC LVT")          # переехал в ключ
        self.assertEqual(rows[0]["supplier"], "Монарх Логистик")
        self.assertEqual(rows[0]["uses"], 2)                    # счётчик не сброшен
        self.assertEqual(rows[0]["mapping"]["purchase_column"], "Цена за м2")

    async def test_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            store = PricingStore(path)
            await store.init()
            await store.save_mapping("sig", "X", {"purchase_column": "a"}, "LA")
            await store.init()                                  # повторный запуск бота
            rows = await store.list_mappings()
        self.assertEqual([r["sheet"] for r in rows], ["LA"])


class ProposalGuardsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec([item("YO-1", 949, 1649, 1139)])
        self.tools = PricingTools(self.onec, self.store, user_id=42)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _propose(self, tm="000000298", coll="YO-C"):
        return await self.tools.execute("propose_prices", {
            "supplier": "Монарх Логистик",
            "groups": [{"tm_code": tm, "tm_name": "Peli", "collection_ref": coll,
                        "purchase": 999, "rrc": 1649}]})

    async def test_out_of_scope_category_is_not_written(self):
        """Даже если модель передала коллекцию, вне списка категорий цены не трогаем."""
        await self.store.add_scope(["обои"])          # ламинат в список не входит
        text = await self._propose()
        self.assertIn("категория не в списке анализируемых", text)
        self.assertIn("/categories", text)
        self.assertIn("Записывать нечего", text)

    async def test_in_scope_category_passes(self):
        await self.store.add_scope(["ламинат"])
        text = await self._propose()
        self.assertNotIn("не в списке анализируемых", text)
        self.assertIn("К записи:", text)

    async def test_loaded_but_unproposed_tm_is_reported(self):
        """Загруженную из 1С ТМ нельзя потерять молча — это и был случай AGT."""
        await self.tools.execute("get_1c_nomenclature", {"tm_code": "000000303"})
        text = await self._propose(tm="000000298")
        self.assertIn("000000303", text)
        self.assertIn("не попало ни одной", text)

    async def test_proposed_tm_is_not_reported(self):
        await self.tools.execute("get_1c_nomenclature", {"tm_code": "000000298"})
        text = await self._propose(tm="000000298")
        self.assertNotIn("не попало ни одной", text)

    async def test_out_of_scope_tm_is_not_reported_as_lost(self):
        """ТМ целиком вне анализируемых категорий пропущена намеренно — не шумим."""
        await self.store.add_scope(["обои"])
        self.tools._onec = FakeOnec([item("YO-9", 100, coll="YO-Z")])
        await self.tools.execute("get_1c_nomenclature", {"tm_code": "000000777"})
        text = await self._propose(tm="000000777", coll="YO-Z")
        self.assertNotIn("не попало ни одной", text)

    async def test_replacing_pending_proposal_warns(self):
        """Старая кнопка перестаёт работать — админ должен узнать об этом сразу."""
        first = await self._propose()
        self.assertIn("К записи:", first)
        second = await self._propose()
        self.assertIn("Прежнее предложение", second)
        self.assertIn("кнопка под ним больше не сработает", second)

    async def test_first_proposal_does_not_warn(self):
        self.assertNotIn("Прежнее предложение", await self._propose())


if __name__ == "__main__":
    unittest.main()
