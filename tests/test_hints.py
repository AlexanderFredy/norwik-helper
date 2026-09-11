"""Подсказки админу курсивом в конце сообщения (§19.7).

Бот показывает состояние, но не всегда очевидно, ЧТО от админа требуется: после обрыва
связи — написать «продолжай», под предложением с вопросом — ответить текстом, а не нажать
кнопку. Нажатие записало бы в 1С версию БЕЗ ответа, и правка уехала бы отдельным кругом.

Курсив задаётся `entities`, а не разметкой: разметка потребовала бы экранировать весь текст
предложения, где живут «ёлочки», стрелки и амперсанды из имён коллекций.
"""
import tempfile
import unittest
from pathlib import Path

from src.bot import pricing_handlers as ph
from src.storage.pricing import PricingStore


class Chat:
    """Заглушка сообщения: запоминает, что и с какой разметкой ушло."""

    def __init__(self):
        self.sent: list[tuple[str, object]] = []
        self.edited: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None, entities=None, parse_mode=None):
        self.sent.append((text, entities))
        return self

    async def edit_text(self, text, entities=None, reply_markup=None):
        self.edited.append((text, entities))
        return self


def _italic(entities) -> list:
    return [e for e in (entities or []) if e.type == "italic"]


class WithHintTest(unittest.TestCase):

    def test_hint_is_appended_and_marked_italic(self):
        text, entities = ph._with_hint("Коллекция Vintage\nПравок: 5", "Напишите текстом")
        self.assertTrue(text.endswith("Напишите текстом"))
        mark = _italic(entities)[0]
        self.assertEqual(text.encode("utf-16-le")[mark.offset * 2:].decode("utf-16-le"),
                         "Напишите текстом")

    def test_offset_is_correct_past_non_bmp_characters(self):
        """Смещение считается в UTF-16: эмодзи вне BMP занимают две единицы, и обычная
        длина строки Python дала бы сдвиг."""
        body = "🕐 Отложено 🕐 ещё раз"
        text, entities = ph._with_hint(body, "подсказка")
        mark = _italic(entities)[0]
        cut = text.encode("utf-16-le")[mark.offset * 2:
                                       (mark.offset + mark.length) * 2].decode("utf-16-le")
        self.assertEqual(cut, "подсказка")

    def test_markup_characters_are_not_escaped(self):
        """`Onyx&More` — настоящее имя коллекции; экранирование его бы исказило."""
        text, _ = ph._with_hint("Коллекция Onyx&More <60x60>", "подсказка")
        self.assertIn("Onyx&More <60x60>", text)

    def test_empty_hint_changes_nothing(self):
        text, entities = ph._with_hint("Текст", "")
        self.assertEqual(text, "Текст")
        self.assertIsNone(entities)


class SendTest(unittest.IsolatedAsyncioTestCase):

    async def test_hint_goes_with_the_last_chunk(self):
        chat = Chat()
        await ph._send(chat, "x" * 5000, hint="подсказка")
        self.assertEqual(len(chat.sent), 2)
        self.assertIsNone(chat.sent[0][1])                 # в первом куске подсказки нет
        self.assertTrue(chat.sent[1][0].endswith("подсказка"))
        self.assertTrue(_italic(chat.sent[1][1]))

    async def test_hint_survives_a_full_last_chunk(self):
        """Если подсказка не влезает в лимит — уходит отдельным сообщением, но не теряется."""
        chat = Chat()
        await ph._send(chat, "y" * 4096, hint="подсказка")
        self.assertEqual(chat.sent[-1][0], "подсказка")
        self.assertTrue(_italic(chat.sent[-1][1]))

    async def test_no_entities_when_no_hint(self):
        """Без подсказки вызов должен остаться ровно прежним."""
        chat = Chat()
        await ph._send(chat, "Просто текст")
        self.assertEqual(chat.sent, [("Просто текст", None)])


class ResumeHintTest(unittest.IsolatedAsyncioTestCase):
    """После обрыва связи админу нужно знать, чем продолжить, — берём из плана."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_names_the_next_step(self):
        await self.store.start_run(42, "Most Floor", "price.pdf",
                                   [{"code": "T1", "name": "Most Flooring"},
                                    {"code": "T2", "name": "Peli"}])
        hint = await ph._resume_hint(self.store, 42)
        self.assertIn("продолжай с Most Flooring", hint)

    async def test_works_without_a_run(self):
        hint = await ph._resume_hint(self.store, 42)
        self.assertIn("продолжай", hint)
        self.assertNotIn("с с", hint)

    async def test_says_the_file_need_not_be_resent(self):
        hint = await ph._resume_hint(self.store, 42)
        self.assertIn("присылать заново не нужно", hint)
        # Оговорки про перезапуск быть не должно: прайс поднимается при старте (§9.8).
        self.assertNotIn("перезапускали", hint)


class ErrorTextTest(unittest.TestCase):
    """Текст ошибки называет причину; как продолжить — дело подсказки.

    Совет «пришлите файл заново» жил в сообщении о нехватке средств и пережил появление
    сохранения прайсов на диск: в одном сообщении админ читал «пришлите заново» и тут же
    «присылать заново не нужно».
    """

    def test_billing_message_gives_no_stale_recovery_advice(self):
        from src.bot.errors import _BILLING
        self.assertIn("пополните баланс", _BILLING)
        self.assertNotIn("пришлите файл заново", _BILLING)

    def test_error_and_hint_do_not_contradict(self):
        from src.bot.errors import _BILLING
        text, _ = ph._with_hint(_BILLING, hint="Файл присылать заново не нужно.")
        self.assertEqual(text.count("присылать заново"), 1)
        self.assertNotIn("пришлите файл заново", text)


if __name__ == "__main__":
    unittest.main()
