"""Команда /help, меню команд Telegram и проход команд сквозь диалог по прайсу."""
import unittest

from src.bot import (catalog_handlers as ch, handlers, model_handlers as mh,
                     pricing_handlers as ph)
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
        for admin_only in ("/prices", "/suppliers", "/adduser"):
            self.assertNotIn(admin_only, text)

    def test_admin_sees_model_and_catalogue(self):
        """Старый прайсовый поток отключён от ТГ на время обкатки модели, и справка
        описывает то, что реально работает: иначе админ читает про несуществующее."""
        text = build_help(is_admin=True)
        for cmd in ("/prices", "/tasks", "/run", "/rebuild",
                    "/suppliers", "/supplier_merge",
                    "/adduser", "/removeuser", "/listusers"):
            self.assertIn(cmd, text)

    def test_admin_is_warned_that_execution_is_a_stub(self):
        """Заглушка должна быть названа прямо: иначе «выполнено» прочтут как запись в 1С."""
        text = build_help(is_admin=True)
        self.assertIn("ЗАГЛУШКА", text)
        self.assertIn("в 1С ничего не пишется", text)

    def test_disabled_flow_is_not_advertised(self):
        text = build_help(is_admin=True)
        for gone in ("/cancel_price", "/mappings", "/deferred", "Записать в 1С"):
            self.assertNotIn(gone, text)

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
        self.assertIn("prices", admin)
        # Команды отключённого потока в меню не показываем: то, что не отвечает, хуже
        # показывать, чем не показывать вовсе.
        self.assertNotIn("cancel_price", admin)

    def test_every_menu_command_has_handler(self):
        registered = set()
        # Забыть здесь новый роутер — значит перестать замечать команду в меню без
        # обработчика, ради чего тест и написан.
        for router in (handlers.router, ph.router, ch.router, mh.router):
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
        self.assertIn("/prices", admin_msg.sent[0])

        mgr_msg = FakeMessage()
        await handlers.cmd_help(mgr_msg, is_admin=False, onec=object())
        self.assertNotIn("/prices", mgr_msg.sent[0])


if __name__ == "__main__":
    unittest.main()
