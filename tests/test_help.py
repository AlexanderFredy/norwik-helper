"""Команда /help, меню команд Telegram и проход команд сквозь диалог по прайсу."""
import unittest

from src.bot import handlers, pricing_handlers as ph
from src.bot.commands import ADMIN_COMMANDS, COMMON_COMMANDS, build_help


class FakeMessage:
    def __init__(self, text: str = "", user_id: int = 42):
        self.from_user = type("U", (), {"id": user_id})()
        self._text = text
        self.sent: list[str] = []

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.sent.append(text)
        return self

    @property
    def text(self) -> str:
        return self._text


class HelpTextTest(unittest.TestCase):
    def test_manager_sees_only_search(self):
        text = build_help(is_admin=False)
        self.assertIn("Поиск товара", text)
        for admin_only in ("/cancel_price", "/mappings", "/adduser", "1С"):
            self.assertNotIn(admin_only, text)

    def test_admin_sees_price_mode(self):
        text = build_help(is_admin=True)
        for cmd in ("/cancel_price", "/mappings", "/mapping_forget",
                    "/adduser", "/removeuser", "/listusers"):
            self.assertIn(cmd, text)
        self.assertIn("Записать в 1С", text)

    def test_admin_warned_when_1c_off(self):
        self.assertIn("не настроена", build_help(is_admin=True, onec_enabled=False))
        self.assertNotIn("не настроена", build_help(is_admin=True, onec_enabled=True))

    def test_angle_brackets_escaped_for_html_parse_mode(self):
        """Текст уходит с parse_mode=HTML — сырые <номер> сломали бы отправку."""
        self.assertNotIn("<номер>", build_help(is_admin=True))
        self.assertIn("&lt;номер&gt;", build_help(is_admin=True))

    def test_menu_scopes(self):
        common = {c.command for c in COMMON_COMMANDS}
        admin = {c.command for c in ADMIN_COMMANDS}
        self.assertEqual(common, {"start", "help"})
        self.assertTrue(common < admin)
        self.assertIn("mapping_forget", admin)

    def test_every_menu_command_has_handler(self):
        registered = set()
        for router in (handlers.router, ph.router):
            for h in router.message.handlers:
                for f in h.filters:
                    registered |= set(getattr(f.callback, "commands", None) or [])
        for cmd in ADMIN_COMMANDS:
            self.assertIn(cmd.command, registered, f"/{cmd.command} в меню, но без обработчика")


class CommandsDuringPriceDialogTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ph._files[42] = ("price.xlsx", b"x")

    def tearDown(self):
        ph._files.pop(42, None)

    def test_command_not_swallowed_by_price_dialog(self):
        self.assertFalse(ph._is_price_reply(FakeMessage("/help")))
        self.assertFalse(ph._is_price_reply(FakeMessage("/start")))

    def test_plain_answer_still_goes_to_agent(self):
        self.assertTrue(ph._is_price_reply(FakeMessage("бери вторую колонку")))

    def test_no_dialog_no_capture(self):
        self.assertFalse(ph._is_price_reply(FakeMessage("привет", user_id=7)))

    async def test_help_renders_for_admin_and_manager(self):
        admin_msg = FakeMessage()
        await handlers.cmd_help(admin_msg, is_admin=True, onec=object())
        self.assertIn("/mappings", admin_msg.sent[0])

        mgr_msg = FakeMessage()
        await handlers.cmd_help(mgr_msg, is_admin=False, onec=object())
        self.assertNotIn("/mappings", mgr_msg.sent[0])


if __name__ == "__main__":
    unittest.main()
