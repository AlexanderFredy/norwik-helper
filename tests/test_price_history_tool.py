"""Инструмент get_price_history: вопрос менеджера → готовый ответ (dev_tasks п.6)."""
import tempfile
import unittest
from pathlib import Path

from src.agent.tools import TOOL_DEFINITIONS, ToolExecutor
from src.onec.client import NomItem, Nomenclature, Price, TradeMark
from src.price_tool.broadcast import journal_rows
from src.storage.pricing import PricingStore


def item(ref, name, coll, coll_ref, purchase=1050, date="2026-07-20", article=""):
    return NomItem(ref=ref, id="1", name=name, article=article, unit="м2", size="",
                   product_type="Ламинат", collection=coll, parent=coll,
                   collection_ref=coll_ref, alt_units={},
                   purchase=Price(value=float(purchase), date=date), retail=None, rrc=None)


class FakeOnec:
    def __init__(self, items, errors=None):
        self._items = items
        self.errors = errors or []

    def selling_tm(self):
        return [TradeMark(name="Classen / Классен", code="000000104"),
                TradeMark(name="Peli", code="000000298")]

    def by_tm_all(self, tm_code, **kw):
        items = self._items if tm_code == "000000104" else []
        return Nomenclature(tm=tm_code, total=len(items), items=items, errors=self.errors)


ITEMS = ([item(f"YO-a{i}", f"Classen Adventure Дуб {i}", "Adventure", "YO-A") for i in range(5)]
         + [item(f"YO-b{i}", f"Classen Ambience Ясень {i}", "Ambience", "YO-B",
                 purchase=800, date="2026-03-01") for i in range(3)]
         + [item("YO-x", "Classen Adventure Дуб Авола", "Adventure", "YO-A",
                 article="62593")])


class PriceHistoryToolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.tools = ToolExecutor(mail=None, norwik=None, onec=FakeOnec(ITEMS),
                                  pricing_store=self.store)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _ask(self, **kw) -> str:
        return await self.tools.execute("get_price_history", kw)

    async def test_tool_is_declared(self):
        names = {t.get("name") for t in TOOL_DEFINITIONS}
        self.assertIn("get_price_history", names)

    async def test_unknown_tm_lists_available(self):
        text = await self._ask(tm="Kronospan")
        self.assertIn("нет в выгрузке", text)
        self.assertIn("Classen", text)

    async def test_tm_matched_by_cyrillic_half_of_name(self):
        self.assertNotIn("нет в выгрузке", await self._ask(tm="классен"))

    async def test_product_by_article(self):
        text = await self._ask(tm="Classen", product="62593")
        self.assertIn("Classen Adventure Дуб Авола", text)
        self.assertIn("закуп 1 050 от 20.07.2026", text)

    async def test_collection_answer_has_no_item_list(self):
        text = await self._ask(tm="Classen", collection="Ambience")
        self.assertIn("Ambience", text)
        self.assertIn("01.03.2026", text)
        self.assertNotIn("Ясень 1", text)     # про коллекцию — без перечисления товаров

    async def test_unknown_collection(self):
        self.assertIn("не найдена", await self._ask(tm="Classen", collection="Nirvana"))

    async def test_whole_tm_reports_by_collection(self):
        text = await self._ask(tm="Classen")
        self.assertIn("9 товаров в 2 коллекциях", text)
        self.assertIn("Adventure", text)
        self.assertIn("Ambience", text)

    async def test_source_from_journal_appears(self):
        await self.store.record_writes(journal_rows({
            "supplier": "Монарх Логистик", "price_doc": "Монарх-логистик",
            "price_date": "2026-07-15",
            "groups": [{"tm_code": "000000104", "tm_name": "Classen",
                        "collection": "Adventure", "collection_ref": "YO-A",
                        "items": [{"ref": "YO-x", "name": "Авола",
                                   "prices": {"purchase": [980, 1050]}}]}],
        }, "2026-07-20"))
        text = await self._ask(tm="Classen", product="62593")
        self.assertIn("Прайс «Монарх-логистик» от 15.07.2026", text)
        self.assertNotIn("вручную", text)

    async def test_manual_edit_admitted(self):
        text = await self._ask(tm="Classen", product="62593")
        self.assertIn("вручную", text)

    async def test_incomplete_answer_is_flagged(self):
        """Если 1С часть позиций не отдала, менеджеру нельзя отвечать как за полные данные."""
        self.tools = ToolExecutor(
            mail=None, norwik=None, pricing_store=self.store,
            onec=FakeOnec(ITEMS, errors=[{"ref": "YO-z", "code": "item_failed"}]))
        text = await self._ask(tm="Classen", product="62593")
        self.assertIn("1С не отдала 1 поз.", text)

    async def test_without_1c_says_so(self):
        tools = ToolExecutor(mail=None, norwik=None)
        self.assertIn("не настроена", await tools.execute("get_price_history", {"tm": "X"}))

    async def test_missing_product_reported(self):
        self.assertIn("не найден", await self._ask(tm="Classen", product="Нирвана 777"))

    async def test_other_tools_still_work(self):
        """Новый инструмент не должен ломать разбор остальных имён."""
        self.assertIn("Неизвестный инструмент",
                      await self.tools.execute("no_such_tool", {}))


if __name__ == "__main__":
    unittest.main()
