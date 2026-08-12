"""Уведомление менеджеров и журнал записей цен (dev_tasks п.6)."""
import tempfile
import unittest
from pathlib import Path

from src.agent.pricing_tools import PricingTools
from src.bot import pricing_handlers as ph
from src.price_tool.broadcast import build_broadcast, journal_rows
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec, item

DIGEST = {
    "supplier": "Монарх Логистик", "price_doc": "Монарх-логистик",
    "price_date": "2026-07-15",
    "groups": [
        {"tm_code": "T1", "tm_name": "Classen", "collection": "Adventure",
         "collection_ref": "YO-A",
         "items": [{"ref": f"YO-a{i}", "name": f"Товар {i}",
                    "prices": {"purchase": [980, 1050], "rrc": [1400, 1550],
                               "retail": [1100, 1200]}} for i in range(5)]},
        {"tm_code": "T1", "tm_name": "Classen", "collection": "Ambience",
         "collection_ref": "YO-B",
         "items": [{"ref": f"YO-b{i}", "name": f"Товар B{i}",
                    "prices": {"purchase": [700, 800]}} for i in range(15)]},
    ],
}


class BroadcastTest(unittest.TestCase):
    def test_message_matches_required_shape(self):
        text = build_broadcast(DIGEST)
        self.assertIn("Поменял цены на Classen (20 товаров):", text)
        self.assertIn("- Adventure — 5 товаров "
                      "(закуп 980 → 1 050, РРЦ 1 400 → 1 550, наша роз. 1 100 → 1 200)", text)
        self.assertIn("- Ambience — 15 товаров (закуп 700 → 800)", text)
        self.assertIn("Цены брал из прайса «Монарх-логистик» от 15.07.2026", text)

    def test_failed_positions_excluded(self):
        """О цене, которую 1С не приняла, менеджеру сообщать нельзя."""
        text = build_broadcast(DIGEST, failed_refs={f"YO-a{i}" for i in range(5)})
        self.assertNotIn("Adventure", text)
        self.assertIn("Classen (15 товаров)", text)

    def test_all_failed_means_no_message(self):
        refs = {i["ref"] for g in DIGEST["groups"] for i in g["items"]}
        self.assertIsNone(build_broadcast(DIGEST, failed_refs=refs))

    def test_first_price_shown_as_new(self):
        digest = {"groups": [{"tm_name": "TM", "collection": "C",
                              "items": [{"ref": "R", "prices": {"purchase": [None, 500]}}]}]}
        self.assertIn("закуп нет → 500", build_broadcast(digest))

    def test_pointwise_prices_do_not_fake_a_single_transition(self):
        digest = {"groups": [{"tm_name": "TM", "collection": "C", "items": [
            {"ref": "R1", "prices": {"purchase": [100, 200]}},
            {"ref": "R2", "prices": {"purchase": [300, 400]}}]}]}
        self.assertIn("закуп у 2 поз.", build_broadcast(digest))

    def test_same_new_price_from_different_old(self):
        digest = {"groups": [{"tm_name": "TM", "collection": "C", "items": [
            {"ref": "R1", "prices": {"purchase": [100, 400]}},
            {"ref": "R2", "prices": {"purchase": [300, 400]}}]}]}
        self.assertIn("закуп → 400", build_broadcast(digest))


class JournalTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_rows_cover_every_price_type(self):
        rows = journal_rows(DIGEST, "2026-07-20")
        self.assertEqual(len(rows), 5 * 3 + 15)
        self.assertEqual(rows[0]["price_doc"], "Монарх-логистик")
        self.assertEqual(rows[0]["written_on"], "2026-07-20")

    async def test_source_found_only_for_matching_date(self):
        await self.store.record_writes(journal_rows(DIGEST, "2026-07-20"))
        found = await self.store.price_sources(["YO-a0"], ["2026-07-20"])
        self.assertEqual(found[("YO-a0", "2026-07-20")]["price_doc"], "Монарх-логистик")
        # 1С показывает другую дату — значит цену меняли не мы
        self.assertEqual(await self.store.price_sources(["YO-a0"], ["2026-05-01"]), {})

    async def test_failed_positions_not_journaled(self):
        rows = journal_rows(DIGEST, "2026-07-20", failed_refs={"YO-a0"})
        self.assertNotIn("YO-a0", [r["item_ref"] for r in rows])

    async def test_digest_survives_confirmation(self):
        """Дайджест должен доехать до кнопки: после подтверждения пересчитать его негде."""
        onec = FakeOnec([item("YO-1", 949, 1649, 1139)])
        tools = PricingTools(onec, self.store, user_id=42)
        await tools.execute("propose_prices", {
            "supplier": "LINDERWOOD", "price_doc": "ЛИНДЕРВУД", "price_date": "2026-08-01",
            "groups": [{"tm_code": "T", "tm_name": "Peli", "collection_ref": "YO-C",
                        "purchase": 999}]})
        pending = await self.store.get_pending(42)
        taken = await self.store.take_pending(42, pending.proposal_id)
        self.assertEqual(taken.digest["price_doc"], "ЛИНДЕРВУД")
        text = build_broadcast(taken.digest)
        self.assertIn("Поменял цены на Peli (1 товаров)", text)
        self.assertIn("закуп 949 → 999", text)
        self.assertIn("«ЛИНДЕРВУД» от 01.08.2026", text)


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text):
        if chat_id == 666:
            raise RuntimeError("bot was blocked by the user")
        self.sent.append((chat_id, text))


class FakeUserStore:
    def __init__(self, ids):
        self._ids = ids

    async def list_all(self):
        return [type("U", (), {"telegram_id": i})() for i in self._ids]


class NotifyTest(unittest.IsolatedAsyncioTestCase):
    async def test_admin_excluded_and_blocked_user_survived(self):
        bot = FakeBot()
        sent = await ph._notify_managers(bot, FakeUserStore([7, 8, 666, 42]), DIGEST,
                                         {"errors": []}, admin_id=42)
        self.assertEqual(sent, 2)
        self.assertEqual([c for c, _ in bot.sent], [7, 8])

    async def test_write_date_falls_back_to_today(self):
        self.assertEqual(ph._written_on({"date": "2026-07-20"}), "2026-07-20")
        self.assertEqual(len(ph._written_on({"date": "20 июля 2026"})), 10)


if __name__ == "__main__":
    unittest.main()
