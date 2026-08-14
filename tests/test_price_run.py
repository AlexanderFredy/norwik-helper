"""Прогон прайса по маркам (§9.6): план, по одной ТМ за раз, продолжение после кнопки.

Раньше предложение приходило одной простынёй по всему прайсу: 62 строки запроса, отчёт
на 10 000 символов и «+102%» Classen, потерянные среди трёх десятков коллекций. Теперь
марка обрабатывается целиком — показ, вопросы, запись — и только потом следующая.
"""
import tempfile
import unittest
from pathlib import Path

from src.agent.pricing_tools import PricingTools, clear_nomenclature_cache
from src.bot import pricing_handlers as ph
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec, item

TMS = [{"code": "T1", "name": "Egger"}, {"code": "T2", "name": "Classen"},
       {"code": "T3", "name": "AGT"}]


class RunStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_plan_then_shrinks(self):
        await self.store.start_run(42, "Монарх", "Монарх-логистик", TMS)
        run = await self.store.get_run(42)
        self.assertEqual([t["name"] for t in run["remaining"]], ["Egger", "Classen", "AGT"])

        run = await self.store.mark_tm_done(42, "T1")
        self.assertEqual([t["name"] for t in run["remaining"]], ["Classen", "AGT"])
        run = await self.store.mark_tm_done(42, "T2")
        self.assertEqual([t["name"] for t in run["remaining"]], ["AGT"])
        run = await self.store.mark_tm_done(42, "T3")
        self.assertEqual(run["remaining"], [])

    async def test_new_run_replaces_old(self):
        """Новый прайс — новый план, хвосты прошлого не должны всплывать."""
        await self.store.start_run(42, "A", "a.xlsx", TMS)
        await self.store.mark_tm_done(42, "T1")
        await self.store.start_run(42, "B", "b.xlsx", [{"code": "X", "name": "Peli"}])
        run = await self.store.get_run(42)
        self.assertEqual(run["done"], set())
        self.assertEqual([t["name"] for t in run["remaining"]], ["Peli"])

    async def test_clear_and_absent(self):
        self.assertIsNone(await self.store.get_run(42))
        self.assertIsNone(await self.store.mark_tm_done(42, "T1"))
        await self.store.start_run(42, "A", "a", TMS)
        await self.store.clear_run(42)
        self.assertIsNone(await self.store.get_run(42))


class ProposePerTmTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec([item("YO-1", 949, 1649, 1139)])
        self.tools = PricingTools(self.onec, self.store, user_id=42)
        await self.store.start_run(42, "Монарх", "Монарх-логистик", TMS)

    async def asyncTearDown(self):
        self._dir.cleanup()

    def _group(self, tm, purchase=999):
        return {"tm_code": tm, "tm_name": tm, "collection_ref": "YO-C",
                "purchase": purchase, "rrc": 1649}

    async def test_start_run_tool_names_first_tm(self):
        text = await self.tools.execute("start_price_run", {
            "supplier": "Монарх", "price_doc": "Монарх-логистик", "trademarks": TMS})
        self.assertIn("3 марок", text)
        self.assertIn("начни с «Egger»", text)

    async def test_two_tms_at_once_refused(self):
        text = await self.tools.execute("propose_prices", {
            "groups": [self._group("T1"), self._group("T2")]})
        self.assertIn("Обрабатывай по одной", text)
        self.assertIsNone(await self.store.get_pending(42))

    async def test_single_tm_lists_whats_left(self):
        text = await self.tools.execute("propose_prices", {"groups": [self._group("T1")]})
        self.assertIn("Осталось обработать: Classen, AGT.", text)
        self.assertIn("НЕ переходи", text)
        self.assertIsNotNone(await self.store.get_pending(42))

    async def test_nothing_to_write_closes_tm_and_moves_on(self):
        """Кнопки не будет — значит марку закрываем сами, иначе прогон встанет."""
        text = await self.tools.execute("propose_prices",
                                        {"groups": [self._group("T1", purchase=949)]})
        self.assertIn("продолжай со следующей марки: Classen", text)
        run = await self.store.get_run(42)
        self.assertEqual([t["name"] for t in run["remaining"]], ["Classen", "AGT"])

    async def test_last_tm_without_changes_finishes_the_run(self):
        for code in ("T1", "T2"):
            await self.store.mark_tm_done(42, code)
        text = await self.tools.execute("propose_prices",
                                        {"groups": [self._group("T3", purchase=949)]})
        self.assertIn("Прайс «Монарх-логистик» обработан полностью", text)

    async def test_works_without_a_run(self):
        """Прогон мог не стартовать (простой однобрендовый прайс) — не падаем."""
        await self.store.clear_run(42)
        text = await self.tools.execute("propose_prices", {"groups": [self._group("T1")]})
        self.assertNotIn("Осталось обработать", text)
        self.assertIn("К записи:", text)


class FakeCallback:
    def __init__(self, proposal_id, message, user_id=42):
        self.data = f"price:apply:{proposal_id}"
        self.from_user = type("U", (), {"id": user_id})()
        self.message = message
        self.answers: list[str] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)


class FakeChat:
    def __init__(self):
        self.sent: list[str] = []
        self.bot = None

    async def answer(self, text, reply_markup=None):
        self.sent.append(text)
        return self

    async def edit_text(self, text, reply_markup=None):
        self.sent.append(text)

    async def edit_reply_markup(self, reply_markup=None):
        pass

    async def delete(self):
        pass


class FakeOrchestrator:
    def __init__(self):
        self.prompts: list[str] = []

    async def handle_turn(self, history, on_tool=None, system=None, extra_tools=None,
                          extra_executor=None):
        self.prompts.append(history[-1]["content"])
        return "продолжаю", history


class FakeUsers:
    async def list_all(self):
        return []


class ContinueAfterApplyTest(unittest.IsolatedAsyncioTestCase):
    """Кнопка нажата — цикл должен сам поехать к следующей марке."""

    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec([item("YO-1", 949, 1649, 1139)])
        self.tools = PricingTools(self.onec, self.store, user_id=42)
        await self.store.start_run(42, "Монарх", "Монарх-логистик", TMS)
        await self.tools.execute("propose_prices", {
            "groups": [{"tm_code": "T1", "tm_name": "Egger", "collection_ref": "YO-C",
                        "purchase": 999}]})
        self.pending = await self.store.get_pending(42)
        self.chat = FakeChat()
        self.orc = FakeOrchestrator()

    async def asyncTearDown(self):
        self._dir.cleanup()
        ph._files.pop(42, None)

    async def _press(self):
        cb = FakeCallback(self.pending.proposal_id, self.chat)
        await ph.handle_price_decision(cb, self.onec, self.store, FakeUsers(),
                                       is_admin=True, orchestrator=self.orc)
        return cb

    async def test_next_tm_is_started_automatically(self):
        await self._press()
        text = "\n".join(self.chat.sent)
        self.assertIn("Осталось обработать: Classen, AGT.", text)
        self.assertIn("Перехожу к марке Classen", text)
        self.assertIn("Продолжай со следующей марки: Classen", self.orc.prompts[-1])

    async def test_price_mode_stays_open_until_the_end(self):
        """Файл и план не должны сбрасываться на середине прайса."""
        ph._files[42] = ("monarh.xlsx", b"x")
        await self._press()
        self.assertIn(42, ph._files)
        self.assertIsNotNone(await self.store.get_run(42))

    async def test_last_tm_finishes_the_run(self):
        for code in ("T2", "T3"):
            await self.store.mark_tm_done(42, code)
        ph._files[42] = ("monarh.xlsx", b"x")
        await self._press()
        text = "\n".join(self.chat.sent)
        self.assertIn("Прайс «Монарх-логистик» обработан полностью", text)
        self.assertNotIn(42, ph._files)
        self.assertIsNone(await self.store.get_run(42))
        self.assertEqual(self.orc.prompts, [])          # продолжать нечего


if __name__ == "__main__":
    unittest.main()
