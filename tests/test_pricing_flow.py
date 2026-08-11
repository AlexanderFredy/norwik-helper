"""Сценарий режима цен без Telegram и без сети: предложение → сохранение → payload.

Проверяется ключевое свойство гейта (§10): в 1С уходит РОВНО тот payload, который был
сохранён при показе предложения, и только один раз.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from src.agent.pricing_tools import PricingTools
from src.onec.client import NomItem, Price
from src.storage.pricing import PricingStore


def item(ref, purchase, rrc=None, retail=None, coll="YO-C"):
    def p(v, dt="2026-08-01"):
        return Price(value=float(v), date=dt) if v is not None else None
    return NomItem(ref=ref, id="1", name=f"Товар {ref}", article="", unit="м2", size="",
                   product_type="Ламинат", collection="Vintage", parent="Vintage",
                   collection_ref=coll, alt_units={}, purchase=p(purchase),
                   retail=p(retail), rrc=p(rrc))


class FakeOnec:
    def __init__(self, items):
        self._items = items
        self.written: list[list[dict]] = []

    def by_tm_all(self, tm_code, **kw):
        return self._items

    def set_prices(self, items):
        self.written.append(items)
        return {"date": "2026-08-11", "updated": len(items), "results": [], "errors": []}


class PricingFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec([item("YO-1", 949, 1649, 1139), item("YO-2", 949, 1649, 1139)])
        self.tools = PricingTools(self.onec, self.store, user_id=42)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _propose(self, purchase=999, rrc=1649):
        return await self.tools.execute("propose_prices", {
            "supplier": "LINDERWOOD",
            "groups": [{"tm_code": "000000298", "tm_name": "Peli",
                        "collection_ref": "YO-C", "purchase": purchase, "rrc": rrc}],
        })

    async def test_proposal_saved_with_payload(self):
        text = await self._propose()
        self.assertIn("Обновляем цены от LINDERWOOD", text)
        pending = await self.store.get_pending(42)
        self.assertIsNotNone(pending)
        # закупка выросла на 5.3% → пишем; розница 999×1.2 = 1199
        self.assertEqual(pending.payload[0]["prices"]["purchase"], 999.0)
        self.assertEqual(pending.payload[0]["prices"]["retail"], 1199.0)
        self.assertIn("collection_ref", pending.payload[0])       # форма «а»

    async def test_kopeck_change_produces_no_proposal(self):
        text = await self._propose(purchase=950, rrc=1649)        # +0.1% → ниже порога
        self.assertIsNone(await self.store.get_pending(42))
        self.assertIn("Записывать нечего", text)

    async def test_apply_uses_saved_payload_once(self):
        await self._propose()
        pending = await self.store.get_pending(42)
        taken = await self.store.take_pending(42, pending.proposal_id)
        self.assertEqual(taken.payload, pending.payload)
        self.onec.set_prices(taken.payload)
        self.assertEqual(self.onec.written[0], pending.payload)
        # повторное нажатие кнопки не должно ничего записать
        self.assertIsNone(await self.store.take_pending(42, pending.proposal_id))

    async def test_new_proposal_supersedes_old(self):
        await self._propose(purchase=999)
        first = await self.store.get_pending(42)
        await self._propose(purchase=1200)
        second = await self.store.get_pending(42)
        self.assertNotEqual(first.proposal_id, second.proposal_id)
        self.assertIsNone(await self.store.take_pending(42, first.proposal_id))

    async def test_cancel_clears_pending(self):
        await self._propose()
        pending = await self.store.get_pending(42)
        self.assertTrue(await self.store.reject(42, pending.proposal_id))
        self.assertIsNone(await self.store.get_pending(42))

    async def test_dialog_history_roundtrip(self):
        await self.store.save_messages(42, [{"role": "user", "content": "привет"}])
        self.assertEqual(await self.store.load_messages(42),
                         [{"role": "user", "content": "привет"}])
        await self.store.reset(42)
        self.assertEqual(await self.store.load_messages(42), [])


if __name__ == "__main__":
    unittest.main()
