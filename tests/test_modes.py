"""Режимы работы агента (§5.2): что запускается и какие замечания видит админ.

Режим управляет не только набором этапов. В режиме «только цены» из предложения уходят
замечания по справочнику — расхождения упаковки, несопоставленные строки, коллекции 1С без
строк в прайсе, — и предложение становится короче и дешевле.

Отдельно закреплено, что НЕ зависит от режима: «1С не отдала N поз.», «категория не в
списке» и потеря загруженной марки. Это состояние системы, а не замечания по прайсу.
"""
import tempfile
import unittest
from pathlib import Path

from src.agent.pricing_tools import PricingTools, clear_nomenclature_cache
from src.bot import pricing_handlers as ph
from src.price_tool import modes
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec, item

TM, COLL = "000000298", "YO-C"


class ParseTest(unittest.TestCase):
    def test_canonical_values(self):
        for value in modes.ALL:
            self.assertEqual(modes.parse(value), value)

    def test_admin_shorthands(self):
        self.assertEqual(modes.parse("  Товары + Цены "), modes.ITEMS_PRICES)
        self.assertEqual(modes.parse("ЦЕНЫ"), modes.PRICES_ONLY)
        self.assertEqual(modes.parse("справочник"), modes.ITEMS_ONLY)

    def test_unknown(self):
        self.assertIsNone(modes.parse("что-нибудь"))
        self.assertIsNone(modes.parse(""))

    def test_flags(self):
        self.assertTrue(modes.with_items(modes.ITEMS_PRICES))
        self.assertTrue(modes.with_prices(modes.ITEMS_PRICES))
        self.assertTrue(modes.with_items(modes.ITEMS_ONLY))
        self.assertFalse(modes.with_prices(modes.ITEMS_ONLY))
        self.assertFalse(modes.with_items(modes.PRICES_ONLY))
        self.assertTrue(modes.with_prices(modes.PRICES_ONLY))


class FakeMessage:
    def __init__(self, user_id=42):
        self.from_user = type("U", (), {"id": user_id})()
        self.sent: list[str] = []

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.sent.append(text)
        return self

    @property
    def text(self) -> str:
        return "\n".join(self.sent)


class Args:
    def __init__(self, args): self.args = args


class CommandTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_default_is_items_and_prices(self):
        msg = FakeMessage()
        await ph.cmd_mode(msg, Args(""), self.store, is_admin=True)
        self.assertIn("правка товаров, потом цены", msg.text)

    async def test_switch_and_persist(self):
        msg = FakeMessage()
        await ph.cmd_mode(msg, Args("цены"), self.store, is_admin=True)
        self.assertIn("только правка цен", msg.text)
        self.assertEqual(await self.store.get_setting(modes.SETTING), modes.PRICES_ONLY)

    async def test_switch_explains_what_changes(self):
        msg = FakeMessage()
        await ph.cmd_mode(msg, Args("цены"), self.store, is_admin=True)
        self.assertIn("показывать больше не буду", msg.text)
        msg = FakeMessage()
        await ph.cmd_mode(msg, Args("товары"), self.store, is_admin=True)
        self.assertIn("кнопки записи в 1С не будет", msg.text)

    async def test_same_mode_says_so(self):
        msg = FakeMessage()
        await ph.cmd_mode(msg, Args("товары+цены"), self.store, is_admin=True)
        self.assertIn("уже такой", msg.text)

    async def test_unknown_mode_rejected(self):
        msg = FakeMessage()
        await ph.cmd_mode(msg, Args("ерунда"), self.store, is_admin=True)
        self.assertIn("Не понял режим", msg.text)
        self.assertEqual(await self.store.get_setting(modes.SETTING, modes.DEFAULT),
                         modes.DEFAULT)

    async def test_manager_denied(self):
        msg = FakeMessage()
        await ph.cmd_mode(msg, Args("цены"), self.store, is_admin=False)
        self.assertIn("только администратору", msg.text)


class ProposalByModeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec([item("YO-1", 949, 1649, 1139)])
        self.tools = PricingTools(self.onec, self.store, user_id=42)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _propose(self, mode, **extra):
        self.tools.mode = mode
        return await self.tools.execute("propose_prices", {
            "supplier": "Монарх", "groups": [
                {"tm_code": TM, "tm_name": "Peli", "collection_ref": COLL,
                 "purchase": 999, "rrc": 1649}], **extra})

    async def test_items_only_refuses_to_price(self):
        text = await self._propose(modes.ITEMS_ONLY)
        self.assertIn("только правка товаров", text)
        self.assertIsNone(await self.store.get_pending(42))

    async def test_prices_only_hides_item_warnings(self):
        text = await self._propose(
            modes.PRICES_ONLY,
            warnings=["⚠️ РРЦ ниже закупки"],
            item_warnings=["не сопоставлены 3 строки прайса"])
        self.assertIn("РРЦ ниже закупки", text)
        self.assertNotIn("не сопоставлены", text)

    async def test_both_modes_show_item_warnings(self):
        for mode in (modes.ITEMS_PRICES,):
            text = await self._propose(mode, item_warnings=["не сопоставлены 3 строки"])
            self.assertIn("не сопоставлены 3 строки", text)

    async def test_system_notes_survive_prices_only(self):
        """Это состояние системы, а не замечания по прайсу — видно в любом режиме."""
        self.onec.errors = [{"ref": "YO-9", "code": "x", "message": "y"}]
        clear_nomenclature_cache()
        text = await self._propose(modes.PRICES_ONLY)
        self.assertIn("1С не отдала", text)

    async def test_category_guard_survives_prices_only(self):
        await self.store.add_scope(["обои"])          # ламинат вне списка
        text = await self._propose(modes.PRICES_ONLY)
        self.assertIn("категория не в списке анализируемых", text)

    async def test_missing_collection_is_an_item_note(self):
        self.tools.mode = modes.PRICES_ONLY
        text = await self.tools.execute("propose_prices", {"groups": [
            {"tm_code": TM, "collection_ref": "YO-НЕТ", "purchase": 999}]})
        self.assertNotIn("не найдена у ТМ", text)

        self.tools.mode = modes.ITEMS_PRICES
        text = await self.tools.execute("propose_prices", {"groups": [
            {"tm_code": TM, "collection_ref": "YO-НЕТ", "purchase": 999}]})
        self.assertIn("не найдена у ТМ", text)

    async def test_mode_is_told_to_the_model(self):
        self.tools.mode = modes.PRICES_ONLY
        self.tools.set_file("p.csv", b"\xd0\x90;1\n")
        text = await self.tools.execute("read_price_file", {})
        self.assertIn("РЕЖИМ РАБОТЫ: только правка цен", text)
        self.assertIn("item_warnings не передавай", text)


if __name__ == "__main__":
    unittest.main()
