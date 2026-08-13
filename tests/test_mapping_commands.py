"""Команды /mappings и /mapping_forget: список запомненных форматов и забывание."""
import tempfile
import unittest
from pathlib import Path

from src.bot import pricing_handlers as ph
from src.storage.pricing import PricingStore


class FakeMessage:
    def __init__(self, user_id=42):
        self.from_user = type("U", (), {"id": user_id})()
        self.sent: list[str] = []

    async def answer(self, text, reply_markup=None):
        self.sent.append(text)
        return self

    @property
    def text(self) -> str:
        return "\n".join(self.sent)


class Args:
    def __init__(self, args): self.args = args


class MappingCommandsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _seed(self):
        await self.store.save_mapping("aaa111", "LINDERWOOD", {
            "purchase_column": "самовывоз", "rrc_column": "*РРЦ за м2",
            "basis": "base_unit", "note": "админ просил минимальные"})
        await self.store.save_mapping("bbb222", "Монарх Логистик",
                                      {"purchase_column": "Цена за м2"})

    async def test_empty_list(self):
        msg = FakeMessage()
        await ph.cmd_mappings(msg, self.store, is_admin=True)
        self.assertIn("пока нет", msg.text)

    async def test_list_shows_columns_and_reason(self):
        await self._seed()
        msg = FakeMessage()
        await ph.cmd_mappings(msg, self.store, is_admin=True)
        self.assertIn("LINDERWOOD", msg.text)
        self.assertIn("закупка «самовывоз»", msg.text)
        self.assertIn("основание: админ просил минимальные", msg.text)
        self.assertIn("Монарх Логистик", msg.text)

    async def test_forget_by_number(self):
        await self._seed()
        entries = await self.store.list_mappings()
        first = entries[0]["signature"]
        msg = FakeMessage()
        await ph.cmd_mapping_forget(msg, Args("1"), self.store, is_admin=True)
        self.assertIn("Забыл формат", msg.text)
        self.assertEqual(await self.store.get_mappings(first), [])
        self.assertEqual(len(await self.store.list_mappings()), 1)

    async def test_forget_by_signature_prefix(self):
        await self._seed()
        msg = FakeMessage()
        await ph.cmd_mapping_forget(msg, Args("aaa1"), self.store, is_admin=True)
        self.assertEqual(await self.store.get_mappings("aaa111"), [])

    async def test_forget_unknown(self):
        await self._seed()
        msg = FakeMessage()
        await ph.cmd_mapping_forget(msg, Args("99"), self.store, is_admin=True)
        self.assertIn("Не нашёл", msg.text)
        self.assertEqual(len(await self.store.list_mappings()), 2)

    async def test_forget_requires_argument(self):
        msg = FakeMessage()
        await ph.cmd_mapping_forget(msg, Args(None), self.store, is_admin=True)
        self.assertIn("Укажите номер", msg.text)

    async def test_commands_are_admin_only(self):
        await self._seed()
        msg = FakeMessage()
        await ph.cmd_mappings(msg, self.store, is_admin=False)
        await ph.cmd_mapping_forget(msg, Args("1"), self.store, is_admin=False)
        self.assertNotIn("LINDERWOOD", msg.text)
        self.assertEqual(len(await self.store.list_mappings()), 2)

    async def test_commands_win_over_active_price_dialog(self):
        """Пока открыт диалог по прайсу, любой текст ловит handle_price_reply —
        команды должны быть зарегистрированы раньше него."""
        names = [h.callback.__name__ for h in ph.router.message.handlers]
        self.assertLess(names.index("cmd_mappings"), names.index("handle_price_reply"))
        self.assertLess(names.index("cmd_mapping_forget"), names.index("handle_price_reply"))
        self.assertLess(names.index("cmd_cancel"), names.index("handle_price_reply"))


if __name__ == "__main__":
    unittest.main()
