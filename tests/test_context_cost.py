"""Стоимость контекста: кеширование истории, чистка выгрузок, сужение номенклатуры.

Цикл ручной: один шаг по коллекции — это несколько запросов к модели, и каждый несёт всю
историю. На прайсе Артисана она разрослась до 494 000 символов (~200 тыс. токенов), из них
224 000 — пять страниц номенклатуры Atlas Concorde Rus, лежавшие в переписке навсегда.
Отсюда и скачок расхода при переходе к следующей коллекции.
"""
import json
import tempfile
import unittest
from pathlib import Path

from src.agent.orchestrator import _cached
from src.agent.pricing_tools import PricingTools, clear_nomenclature_cache
from src.bot.pricing_handlers import DUMP_STUB, NOM_STUB, _prune_file_dumps
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec, item


def sheet_dump(n=400):
    return "=== Лист: Price ===\n" + "строка прайса\t900\t1200\n" * n


def nom_dump(n=200):
    return json.dumps({"tm": "T1", "total": n, "page": 1, "not_returned_by_1c": [],
                       "items": [{"ref": f"YO-{i}", "name": "Плитка " * 6} for i in range(n)]},
                      ensure_ascii=False)


def result(text):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x",
                                         "content": text}]}


class CacheBreakpointTest(unittest.TestCase):
    """Без точки кеширования каждый вызов инструмента оплачивается по полной."""

    def test_breakpoint_on_last_block(self):
        msgs = [{"role": "user", "content": "прайс"}, result("итог")]
        out = _cached(msgs)
        self.assertEqual(out[-1]["content"][0]["cache_control"], {"type": "ephemeral"})

    def test_original_history_stays_clean(self):
        """cache_control не должен попасть в сохраняемую историю и копиться там."""
        msgs = [result("итог")]
        _cached(msgs)
        self.assertNotIn("cache_control", msgs[-1]["content"][0])

    def test_string_content_becomes_block(self):
        out = _cached([{"role": "user", "content": "текст"}])
        self.assertEqual(out[-1]["content"][0]["type"], "text")
        self.assertIn("cache_control", out[-1]["content"][0])

    def test_thinking_tail_is_left_alone(self):
        """API не принимает cache_control на thinking — молча пропускаем ход."""
        msgs = [{"role": "assistant", "content": [{"type": "thinking", "thinking": "…"}]}]
        self.assertIs(_cached(msgs), msgs)

    def test_empty_and_odd_inputs(self):
        self.assertEqual(_cached([]), [])
        self.assertEqual(_cached([{"role": "user", "content": []}])[0]["content"], [])


class PruneTest(unittest.TestCase):
    def test_both_kinds_pruned_independently(self):
        msgs = [result(sheet_dump()), result(nom_dump()),
                result(sheet_dump()), result(nom_dump())]
        pruned = _prune_file_dumps(msgs)
        self.assertEqual(pruned[0]["content"][0]["content"], DUMP_STUB)
        self.assertEqual(pruned[1]["content"][0]["content"], NOM_STUB)
        # свежая выгрузка каждого вида остаётся
        self.assertIn("строка прайса", pruned[2]["content"][0]["content"])
        self.assertIn("not_returned_by_1c", pruned[3]["content"][0]["content"])

    def test_nomenclature_history_shrinks(self):
        """Пять страниц номенклатуры — ровно случай Atlas Concorde Rus."""
        msgs = [result(nom_dump()) for _ in range(5)]
        before, after = len(str(msgs)), len(str(_prune_file_dumps(msgs)))
        self.assertLess(after, before / 4)

    def test_stub_tells_how_to_get_data_back(self):
        self.assertIn("get_1c_nomenclature", NOM_STUB)
        self.assertIn("collection_ref", NOM_STUB)
        self.assertIn("read_price_file", DUMP_STUB)

    def test_small_results_untouched(self):
        msgs = [result("Маппинг сохранён."), result('{"not_returned_by_1c": []}')]
        self.assertEqual(_prune_file_dumps(msgs), msgs)


class NomenclatureFilterTest(unittest.IsolatedAsyncioTestCase):
    """Сужение до коллекции — главный рычаг: одна папка вместо всей марки."""

    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        items = ([item(f"YO-A{i}", 949, coll="YO-ALL") for i in range(40)]
                 + [item(f"YO-D{i}", 949, coll="YO-DRIFT") for i in range(60)])
        self.tools = PricingTools(FakeOnec(items), self.store, user_id=42)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_filter_returns_only_that_collection(self):
        raw = await self.tools.execute("get_1c_nomenclature",
                                       {"tm_code": "T1", "collection_ref": "YO-ALL"})
        data = json.loads(raw)
        self.assertEqual(data["total"], 40)
        self.assertEqual(data["collection_ref"], "YO-ALL")
        self.assertTrue(all(i["collection_ref"] == "YO-ALL" for i in data["items"]))

    async def test_filter_is_much_cheaper(self):
        whole = await self.tools.execute("get_1c_nomenclature", {"tm_code": "T1"})
        one = await self.tools.execute("get_1c_nomenclature",
                                       {"tm_code": "T1", "collection_ref": "YO-ALL"})
        self.assertLess(len(one), len(whole) / 2)

    async def test_without_filter_nothing_changes(self):
        data = json.loads(await self.tools.execute("get_1c_nomenclature", {"tm_code": "T1"}))
        self.assertEqual(data["total"], 100)
        self.assertIsNone(data["collection_ref"])

    async def test_unknown_collection_is_empty_not_everything(self):
        data = json.loads(await self.tools.execute(
            "get_1c_nomenclature", {"tm_code": "T1", "collection_ref": "YO-НЕТ"}))
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["items"], [])


if __name__ == "__main__":
    unittest.main()
