"""Прогон закрывается только по «Да» админа (§9.9).

Разобрать план до конца и закончить работу с прайсом — РАЗНЫЕ события. У админа остаются
задачи по тому же файлу: завести новую марку и дозаполнить по ней товары, вернуться к
коллекции, переспросить агента. Раньше бот закрывал прогон сам, а закрытие стирает и файл,
и историю диалога, — после чего текст админа уходил менеджерскому агенту, который про 1С
ничего не умеет и отвечал «внесение данных делает администратор». Со стороны это выглядело
как отказ агента, хотя сообщение просто не дошло до того, кому предназначалось.
"""
import tempfile
import unittest
from pathlib import Path

from src.bot import pricing_handlers as ph
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec, item


class FakeChat:
    def __init__(self):
        self.sent: list[str] = []
        self.markups: list[object] = []

    async def answer(self, text, reply_markup=None, entities=None, parse_mode=None):
        self.sent.append(text)
        self.markups.append(reply_markup)
        return self

    async def edit_text(self, text, reply_markup=None, entities=None):
        self.sent.append(text)

    async def delete(self):
        pass


def buttons(chat) -> list[str]:
    out = []
    for markup in chat.markups:
        for row in getattr(markup, "inline_keyboard", []) or []:
            out += [b.text for b in row]
    return out


class Base(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.chat = FakeChat()
        self.onec = FakeOnec([item("YO-1", 949, 1649, 1139)])
        await self.store.start_run(42, "Монарх", "Price.xlsx",
                                   [{"code": "T1", "name": "Egger"}])
        await self.store.mark_tm_done(42, "T1")
        await ph._remember_file(self.store, 42, "Price.xlsx", b"PK x")

    async def asyncTearDown(self):
        ph._files.pop(42, None)
        self._dir.cleanup()


class AskTest(Base):

    async def test_question_is_asked_instead_of_closing(self):
        closed = await ph._finish_run(self.chat, self.store, 42)
        self.assertFalse(closed)
        text = "\n".join(self.chat.sent)
        self.assertIn("Закончить работу с этим прайсом?", text)
        self.assertIn("Если есть ещё задачи по этому прайсу — напишите", text)

    async def test_only_one_button_and_it_says_yes(self):
        """Кнопки «Нет» намеренно нет: «нет» выражается текстом следующей задачи."""
        await ph._finish_run(self.chat, self.store, 42)
        self.assertEqual(buttons(self.chat), ["✅ Да"])

    async def test_nothing_is_cleared_while_waiting(self):
        await ph._finish_run(self.chat, self.store, 42)
        self.assertIn(42, ph._files)                       # прайс в памяти
        self.assertIsNotNone(await self.store.get_run(42))  # план на месте
        self.assertIsNotNone(await self.store.get_active_price(42))

    async def test_flag_is_set(self):
        await ph._finish_run(self.chat, self.store, 42)
        self.assertTrue((await self.store.get_active_price(42))["awaiting_close"])

    async def test_admin_text_still_reaches_the_price_agent(self):
        """Главное следствие: пока не закрыли, текст админа — ответ по прайсу.

        Именно здесь ломалось раньше: файл забывался, `_is_price_reply` становился ложным,
        и «давай заполняй товар» уходило менеджерскому агенту.
        """
        await ph._finish_run(self.chat, self.store, 42)
        msg = type("M", (), {"from_user": type("U", (), {"id": 42})(),
                             "text": "заполни A+ FLOOR"})()
        self.assertTrue(ph._is_price_reply(msg))

    async def test_run_is_not_closed_twice_by_repeated_ends(self):
        await ph._finish_run(self.chat, self.store, 42)
        await ph._finish_run(self.chat, self.store, 42)
        self.assertIn(42, ph._files)
        self.assertEqual("\n".join(self.chat.sent).count("Закончить работу"), 2)


class ConfirmTest(Base):

    async def test_yes_closes_everything(self):
        await ph._finish_run(self.chat, self.store, 42)
        closed = await ph._finish_run(self.chat, self.store, 42, confirmed=True)
        self.assertTrue(closed)
        self.assertNotIn(42, ph._files)
        self.assertIsNone(await self.store.get_run(42))
        self.assertIsNone(await self.store.get_active_price(42))

    async def test_summary_is_not_printed_twice(self):
        """Сводка висит в чате вместе с вопросом; повторять её на «Да» — шум."""
        await ph._finish_run(self.chat, self.store, 42)
        before = len(self.chat.sent)
        await ph._finish_run(self.chat, self.store, 42, confirmed=True)
        tail = "\n".join(self.chat.sent[before:])
        self.assertNotIn("обработан полностью", tail)
        self.assertIn("закончена", tail)

    async def test_quiet_close_does_not_ask_for_the_next_file(self):
        """Закрытие ради нового прайса: «пришлите следующий» тут было бы нелепо."""
        await ph._finish_run(self.chat, self.store, 42)
        before = len(self.chat.sent)
        await ph._finish_run(self.chat, self.store, 42, confirmed=True, quiet=True)
        tail = "\n".join(self.chat.sent[before:])
        self.assertNotIn("Пришлите следующий файл", tail)
        self.assertIn("взялся за новый", tail)
        self.assertNotIn(42, ph._files)


class ReaskTest(Base):
    """После доп. задачи вопрос должен прозвучать заново, а не считаться заданным."""

    async def test_new_work_clears_the_flag(self):
        await ph._finish_run(self.chat, self.store, 42)
        self.assertTrue((await self.store.get_active_price(42))["awaiting_close"])
        await self.store.set_awaiting_close(42, False)      # как делает _run
        self.assertFalse((await self.store.get_active_price(42))["awaiting_close"])

    async def test_question_returns_after_more_work(self):
        await ph._finish_run(self.chat, self.store, 42)
        await self.store.set_awaiting_close(42, False)
        self.chat.sent.clear()
        await ph._finish_run(self.chat, self.store, 42)
        self.assertIn("Закончить работу с этим прайсом?", "\n".join(self.chat.sent))


class GuardTest(Base):

    async def test_unfinished_plan_asks_nothing(self):
        """Пока в плане есть марки, до вопроса дело не доходит вовсе."""
        await self.store.start_run(42, "Монарх", "Price.xlsx",
                                   [{"code": "T1", "name": "Egger"},
                                    {"code": "T2", "name": "Betta"}])
        await self.store.mark_tm_done(42, "T1")
        self.assertFalse(await ph._finish_run(self.chat, self.store, 42))
        self.assertEqual(self.chat.sent, [])


if __name__ == "__main__":
    unittest.main()
