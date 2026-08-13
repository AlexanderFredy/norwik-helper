"""Эксклюзивы от прайса до отчёта: инструмент → SQLite → пометка во всех выводах (§9.5)."""
import tempfile
import unittest
from pathlib import Path

from src.agent.pricing_tools import PricingTools, clear_nomenclature_cache
from src.agent.prompts import SYSTEM_PROMPT, build_system_prompt
from src.bot import pricing_handlers as ph
from src.price_tool.broadcast import build_broadcast
from src.price_tool.exclusive import resolve
from src.price_tool.history import describe_group
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec, item

TM = "000000298"
COLL = "YO-C"


def entry(collection="Vintage", phrase="эксклюзив", where="column", **kw):
    return {"tm_code": TM, "tm_name": "Peli", "collection_ref": COLL,
            "collection": collection, "phrase": phrase, "where_found": where, **kw}


class ExclusiveFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec([item("YO-1", 949, 1649, 1139), item("YO-2", 949, 1649, 1139)])
        self.tools = PricingTools(self.onec, self.store, user_id=42)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _record(self, supplier="Монарх Логистик", price_date="2026-08-01", items=None):
        return await self.tools.execute("record_exclusives", {
            "supplier": supplier, "price_date": price_date,
            "items": items if items is not None else [entry()]})

    async def _propose(self):
        return await self.tools.execute("propose_prices", {
            "supplier": "Монарх Логистик",
            "groups": [{"tm_code": TM, "tm_name": "Peli", "collection_ref": COLL,
                        "purchase": 999, "rrc": 1649}]})

    # ------------------------------------------------------------------ запись

    async def test_claim_recorded_and_labels_proposal(self):
        text = await self._record()
        self.assertIn("Заявки об эксклюзиве записаны: 1", text)
        self.assertIn("Спорных нет", text)
        self.assertIn("Vintage (эксклюзив: Монарх Логистик)", await self._propose())

    async def test_same_price_list_twice_does_not_duplicate(self):
        """Повторный разбор того же прайса не должен плодить заявки."""
        await self._record()
        await self._record()
        claims, _ = await self.store.load_exclusives()
        self.assertEqual(len(claims), 1)

    async def test_word_inside_name_is_not_a_claim(self):
        """«Kronotex Exclusive» — имя коллекции; иначе повесим эксклюзив на весь бренд."""
        text = await self._record(items=[entry(collection="Exclusive")])
        self.assertIn("слово входит в само название", text)
        claims, _ = await self.store.load_exclusives()
        self.assertEqual(claims, [])
        self.assertNotIn("эксклюзив:", await self._propose())

    async def test_unknown_where_found_rejected(self):
        text = await self._record(items=[entry(where="приснилось")])
        self.assertIn("не указано, где найдена надпись", text)
        claims, _ = await self.store.load_exclusives()
        self.assertEqual(claims, [])

    async def test_supplier_required(self):
        text = await self.tools.execute("record_exclusives",
                                        {"supplier": "  ", "items": [entry()]})
        self.assertIn("Не указан поставщик", text)

    # ------------------------------------------------------------------- спор

    async def test_dispute_reported_with_phrases_and_no_label(self):
        await self._record("Монарх Логистик", "2026-07-01")
        text = await self._record("ТД Паркет", "2026-08-01",
                                  items=[entry(phrase="эксклюзивный дистрибьютор")])
        self.assertIn("СПОР", text)
        self.assertIn("Монарх Логистик", text)
        self.assertIn("эксклюзивный дистрибьютор", text)   # админу нужны формулировки
        self.assertIn("set_exclusive", text)
        self.assertNotIn("эксклюзив:", await self._propose())

    async def test_set_exclusive_resolves_dispute(self):
        await self._record("Монарх Логистик", "2026-07-01")
        await self._record("ТД Паркет", "2026-08-01")
        text = await self.tools.execute("set_exclusive", {
            "tm_code": TM, "collection_ref": COLL, "supplier": "ТД Паркет",
            "note": "подтвердил производитель"})
        self.assertIn("ТД Паркет", text)
        self.assertIn("Vintage (эксклюзив: ТД Паркет)", await self._propose())

    async def test_set_exclusive_none_removes_label(self):
        await self._record()
        await self.tools.execute("set_exclusive", {
            "tm_code": TM, "collection_ref": COLL, "supplier": "none"})
        self.assertNotIn("эксклюзив:", await self._propose())

    async def test_forget_survives_new_claim(self):
        """Снятая пометка не должна воскресать от следующего прайса с той же надписью."""
        await self._record()
        await self.store.set_exclusive_decision(TM, COLL, supplier=None)
        await self._record(price_date="2026-08-10")
        active, _ = resolve(*await self.store.load_exclusives())
        self.assertEqual(active, {})

    async def test_clear_decision_restores_claim(self):
        await self._record()
        await self.store.set_exclusive_decision(TM, COLL, supplier=None)
        self.assertTrue(await self.store.clear_exclusive_decision(TM, COLL))
        active, _ = resolve(*await self.store.load_exclusives())
        self.assertEqual(len(active), 1)

    # --------------------------------------------------------- прочие выводы

    async def test_broadcast_labels_collection(self):
        await self._record()
        active, _ = resolve(*await self.store.load_exclusives())
        digest = {"supplier": "Монарх Логистик", "groups": [{
            "tm_code": TM, "tm_name": "Peli", "collection": "Vintage",
            "collection_ref": COLL,
            "items": [{"ref": "YO-1", "name": "Товар", "prices": {"purchase": [949, 999]}}]}]}
        self.assertIn("Vintage (эксклюзив: Монарх Логистик)",
                      build_broadcast(digest, exclusives=active))

    async def test_history_labels_group(self):
        await self._record()
        active, _ = resolve(*await self.store.load_exclusives())
        from src.price_tool.exclusive import find
        text = describe_group("Peli Vintage", [item("YO-1", 949, 1649, 1139)], {},
                              find(active, TM, COLL))
        self.assertIn("Peli Vintage (эксклюзив: Монарх Логистик)", text)

    async def test_system_prompt_carries_exclusives(self):
        await self._record()
        active, _ = resolve(*await self.store.load_exclusives())
        prompt = build_system_prompt(active)
        self.assertIn("Эксклюзивы поставщиков", prompt)
        self.assertIn("Peli / Vintage — Монарх Логистик", prompt)

    async def test_system_prompt_unchanged_without_exclusives(self):
        """Пустой список не должен трогать промпт — иначе зря рвём кеш."""
        self.assertEqual(build_system_prompt({}), SYSTEM_PROMPT)
        self.assertEqual(build_system_prompt(None), SYSTEM_PROMPT)


class FakeMessage:
    def __init__(self, user_id: int = 42):
        self.from_user = type("U", (), {"id": user_id})()
        self.sent: list[str] = []

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.sent.append(text)
        return self


class Args:
    def __init__(self, args=None):
        self.args = args


class ExclusiveCommandsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _claims(self, *rows):
        await self.store.record_exclusive_claims([
            {"supplier": s, "tm_code": TM, "tm_name": "Peli", "collection_ref": COLL,
             "collection": "Vintage", "phrase": "эксклюзив", "where_found": "column",
             "price_date": d} for s, d in rows])

    async def test_empty_list_explains_how_they_appear(self):
        msg = FakeMessage()
        await ph.cmd_exclusives(msg, self.store, is_admin=True)
        self.assertIn("пока нет", msg.sent[0])

    async def test_list_shows_active_and_disputed(self):
        await self._claims(("Монарх Логистик", "2026-07-01"), ("ТД Паркет", "2026-08-01"))
        await self.store.record_exclusive_claims([
            {"supplier": "Дельта", "tm_code": "000000999", "tm_name": "Classen",
             "collection_ref": "YO-X", "collection": "Adventure", "phrase": "только у нас",
             "where_found": "header", "price_date": "2026-08-05"}])
        msg = FakeMessage()
        await ph.cmd_exclusives(msg, self.store, is_admin=True)
        text = "".join(msg.sent)
        self.assertIn("Classen / Adventure — Дельта", text)
        self.assertIn("спорят: Монарх Логистик, ТД Паркет", text)

    async def test_manager_denied(self):
        msg = FakeMessage()
        await ph.cmd_exclusives(msg, self.store, is_admin=False)
        self.assertIn("только администратору", msg.sent[0])

    async def test_forget_writes_decision_not_delete(self):
        """Снятие должно пережить следующий прайс — значит это решение, а не удаление."""
        await self._claims(("Монарх Логистик", "2026-08-01"))
        msg = FakeMessage()
        await ph.cmd_exclusive_forget(msg, Args("1"), self.store, is_admin=True)
        self.assertIn("снята", msg.sent[0])
        claims, decisions = await self.store.load_exclusives()
        self.assertEqual(len(claims), 1)
        self.assertEqual(len(decisions), 1)
        self.assertIsNone(decisions[0].supplier)

    async def test_forget_needs_valid_number(self):
        await self._claims(("Монарх Логистик", "2026-08-01"))
        for arg in (None, "", "0", "2", "первый"):
            msg = FakeMessage()
            await ph.cmd_exclusive_forget(msg, Args(arg), self.store, is_admin=True)
            self.assertIn("Укажите номер", msg.sent[0])


if __name__ == "__main__":
    unittest.main()
