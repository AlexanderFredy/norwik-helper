"""Очередь коллекций внутри крупной марки (§9.6.2).

Прайс «Артисан-Проект»: по Atlas Concorde Rus в 1С 932 позиции, и агент упёрся — вместо
предложения выдал админу три варианта на выбор. Порог теперь проверяет код, и марка сама
разбирается на коллекции, которые идут той же очередью, что и марки.
"""
import tempfile
import unittest
from pathlib import Path

from src.agent.pricing_tools import (
    BIG_TM_ITEMS, PricingTools, clear_nomenclature_cache, next_step, queue_tail,
)
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec, item

TMS = [{"code": "T1", "name": "Atlas Concorde Rus"}, {"code": "T2", "name": "Azteca"}]
COLLS = [{"ref": "YO-A", "name": "Allure"}, {"ref": "YO-D", "name": "Drift"}]


def big(n: int = BIG_TM_ITEMS):
    """Марка крупнее порога: половина позиций в одной коллекции, половина в другой."""
    return [item(f"YO-{i}", 949, 1649, 1139, coll="YO-A" if i % 2 else "YO-D")
            for i in range(n)]


class StageStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        await self.store.start_run(42, "Артисан", "Price.xls", TMS)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_queue_shrinks_then_closes_the_tm(self):
        run = await self.store.start_stage(42, "T1", "Atlas Concorde Rus", COLLS)
        self.assertEqual([c["name"] for c in run["stage"]["remaining"]],
                         ["Allure", "Drift"])

        run = await self.store.mark_collection_done(42, "YO-A")
        self.assertEqual([c["name"] for c in run["stage"]["remaining"]], ["Drift"])
        self.assertEqual([t["name"] for t in run["remaining"]],
                         ["Atlas Concorde Rus", "Azteca"])   # марка ещё в работе

        run = await self.store.mark_collection_done(42, "YO-D")
        self.assertIsNone(run["stage"])                       # второй уровень свернулся
        self.assertEqual([t["name"] for t in run["remaining"]], ["Azteca"])

    async def test_new_run_drops_stage(self):
        await self.store.start_stage(42, "T1", "Atlas", COLLS)
        await self.store.start_run(42, "Другой", "b.xls", TMS)
        self.assertIsNone((await self.store.get_run(42))["stage"])

    async def test_mark_collection_without_stage_is_noop(self):
        run = await self.store.mark_collection_done(42, "YO-A")
        self.assertIsNone(run["stage"])
        self.assertEqual(len(run["remaining"]), 2)


class QueueTailTest(unittest.TestCase):
    def _run(self, stage_left, tms_left):
        return {"stage": {"tm_name": "Atlas Concorde Rus", "tm_code": "T1",
                          "remaining": stage_left} if stage_left is not None else None,
                "remaining": tms_left}

    def test_shows_both_levels(self):
        text = queue_tail(self._run([{"ref": "YO-D", "name": "Drift"}],
                                    [{"code": "T1", "name": "Atlas Concorde Rus"},
                                     {"code": "T2", "name": "Azteca"}]))
        self.assertIn("Сейчас Atlas Concorde Rus. Осталось в марке: Drift.", text)
        self.assertIn("Осталось обработать: Azteca.", text)
        self.assertNotIn("обработать: Atlas", text)     # текущая марка не дублируется

    def test_last_collection_said_plainly(self):
        text = queue_tail(self._run([], [{"code": "T1", "name": "Atlas Concorde Rus"}]))
        self.assertIn("последняя коллекция", text)

    def test_without_stage_only_tms(self):
        text = queue_tail(self._run(None, [{"code": "T1", "name": "A"},
                                           {"code": "T2", "name": "B"}]), skip_tm="T1")
        self.assertIn("Осталось обработать: B.", text)
        self.assertNotIn("Сейчас", text)

    def test_next_step_prefers_collection(self):
        self.assertEqual(next_step(self._run([{"ref": "YO-D", "name": "Drift"}],
                                             [{"code": "T2", "name": "Azteca"}])), "Drift")
        self.assertEqual(next_step(self._run(None, [{"code": "T2", "name": "Azteca"}])),
                         "Azteca")
        self.assertIsNone(next_step(self._run(None, [])))
        self.assertIsNone(next_step(None))


class BigTmSplitTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        await self.store.start_run(42, "Артисан", "Price.xls", TMS)

    async def asyncTearDown(self):
        self._dir.cleanup()

    def _tools(self, items):
        return PricingTools(FakeOnec(items), self.store, user_id=42)

    async def _propose(self, tools, coll="YO-A"):
        return await tools.execute("propose_prices", {"groups": [
            {"tm_code": "T1", "tm_name": "Atlas Concorde Rus",
             "collection_ref": coll, "purchase": 999}]})

    async def test_big_tm_is_refused_until_split(self):
        text = await self._propose(self._tools(big()))
        self.assertIn("разбираем по коллекциям", text)
        self.assertIn("start_tm_collections", text)
        self.assertIsNone(await self.store.get_pending(42))

    async def test_smaller_tm_goes_whole(self):
        """241 позиция (Classen) порога не достигает — дробить незачем."""
        text = await self._propose(self._tools(big(BIG_TM_ITEMS - 1)))
        self.assertNotIn("разбираем по коллекциям", text)
        self.assertIn("К записи:", text)

    async def test_after_split_proposals_go_through(self):
        tools = self._tools(big())
        await tools.execute("start_tm_collections", {
            "tm_code": "T1", "tm_name": "Atlas Concorde Rus", "collections": COLLS})
        text = await self._propose(tools)
        self.assertIn("К записи:", text)
        self.assertIn("Сейчас Atlas Concorde Rus", text)

    async def test_two_collections_at_once_refused(self):
        tools = self._tools(big())
        await tools.execute("start_tm_collections", {
            "tm_code": "T1", "collections": COLLS})
        text = await tools.execute("propose_prices", {"groups": [
            {"tm_code": "T1", "collection_ref": "YO-A", "purchase": 999},
            {"tm_code": "T1", "collection_ref": "YO-D", "purchase": 999}]})
        self.assertIn("ОДНУ коллекцию за вызов", text)

    async def test_collection_without_changes_advances_itself(self):
        tools = self._tools(big())
        await tools.execute("start_tm_collections", {
            "tm_code": "T1", "collections": COLLS})
        text = await tools.execute("propose_prices", {"groups": [
            {"tm_code": "T1", "collection_ref": "YO-A", "purchase": 949}]})
        self.assertEqual(tools.advanced_to, "Drift")
        self.assertIn("продолжай: Drift", text)

    async def test_split_tool_needs_collections(self):
        text = await self._tools(big()).execute("start_tm_collections", {"tm_code": "T1",
                                                                          "collections": []})
        self.assertIn("Не переданы коллекции", text)


if __name__ == "__main__":
    unittest.main()
