"""Кнопки вопроса «где нет фото» и команда `/photo_days` (решение админа 12.10.2026)."""
import unittest
from types import SimpleNamespace

from src.bot import catalog_handlers, handlers


class FakeMessage:
    def __init__(self):
        self.edits: list[tuple] = []
        self.sent: list[str] = []
        self.files: list = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text, reply_markup=None, **_kw):
        self.sent.append(text)

    async def answer_document(self, doc):
        self.files.append(doc)


class FakeCallback:
    def __init__(self, data):
        self.data = data
        self.message = FakeMessage()

    async def answer(self, *_a, **_kw):
        pass


class FakeExecutor:
    def __init__(self, request=None):
        self.requests = {"abcd1234": request} if request is not None else {}
        self.checked: list = []

    def photo_request(self, token):
        return self.requests.get(token)

    def forget_photo_request(self, token):
        self.requests.pop(token, None)

    async def photo_recent_days(self):
        return 180

    async def photo_check(self, request, recent):
        self.checked.append((dict(request), recent))
        return ["отчёт"]


def orchestrator(executor):
    return SimpleNamespace(executor=executor)


class PhotoChoiceTest(unittest.IsolatedAsyncioTestCase):

    async def test_recent_runs_without_category(self):
        ex = FakeExecutor({"type_code": "", "type_name": "", "tms": []})
        cb = FakeCallback("photo:abcd1234:new")
        await handlers.photo_choice(cb, orchestrator(ex))
        self.assertEqual(ex.checked, [({"type_code": "", "type_name": "", "tms": []}, True)])
        self.assertIn("недавно добавленные (за 180 дн)", cb.message.edits[0][0])
        self.assertEqual(cb.message.sent, ["отчёт"])
        self.assertIsNone(ex.photo_request("abcd1234"), "второе нажатие не запустит обход")

    async def test_any_without_category_asks_for_it(self):
        """Весь каталог не обходим: «любые товары» без категории — ещё вопрос, не проверка."""
        ex = FakeExecutor({"type_code": "", "type_name": "", "tms": []})
        cb = FakeCallback("photo:abcd1234:any")
        await handlers.photo_choice(cb, orchestrator(ex))
        self.assertEqual(ex.checked, [])
        text, markup = cb.message.edits[0]
        self.assertEqual(text, "Любые товары — в какой категории смотреть? "
                               "(укажите сразу список брендов)")
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("Ламинат", labels)
        self.assertIsNotNone(ex.photo_request("abcd1234"), "ждём выбора категории")

    async def test_no_brand_hint_when_brands_named(self):
        ex = FakeExecutor({"type_code": "", "type_name": "", "tms": ["Classen"]})
        cb = FakeCallback("photo:abcd1234:any")
        await handlers.photo_choice(cb, orchestrator(ex))
        self.assertEqual(cb.message.edits[0][0], "Любые товары — в какой категории смотреть?")

    async def test_category_button_runs_any(self):
        ex = FakeExecutor({"type_code": "", "type_name": "", "tms": ["Classen"]})
        cb = FakeCallback("photo:abcd1234:t000000003")
        await handlers.photo_choice(cb, orchestrator(ex))
        request, recent = ex.checked[0]
        self.assertFalse(recent)
        self.assertEqual((request["type_code"], request["type_name"]), ("000000003", "Ламинат"))
        self.assertEqual(request["tms"], ["Classen"])

    async def test_any_with_category_runs_at_once(self):
        ex = FakeExecutor({"type_code": "000000011", "type_name": "Керамогранит", "tms": []})
        cb = FakeCallback("photo:abcd1234:any")
        await handlers.photo_choice(cb, orchestrator(ex))
        self.assertEqual(ex.checked[0][1], False)
        self.assertIn("«Керамогранит»", cb.message.edits[0][0])

    async def test_stale_button(self):
        """После перезапуска бота запросов в памяти нет — говорим прямо, а не молчим."""
        cb = FakeCallback("photo:deadbeef:new")
        await handlers.photo_choice(cb, orchestrator(FakeExecutor()))
        self.assertIn("устарел", cb.message.edits[0][0])


class BrandExecutor(FakeExecutor):
    def __init__(self, request, picked=(), unknown=()):
        super().__init__(request)
        self.picked, self.unknown = list(picked), list(unknown)
        self.waits = {}

    def await_photo_brands(self, chat, user, token, message_id):
        self.waits[(chat, user)] = (token, message_id)

    def photo_brand_wait(self, chat, user):
        return self.waits.get((chat, user))

    async def set_photo_brands(self, token, text):
        if self.picked:
            self.requests[token]["tms"] = list(self.picked)
        return self.picked, self.unknown


def text_message(text, reply_to=None):
    msg = FakeMessage()
    msg.text = text
    msg.chat = SimpleNamespace(id=10)
    msg.from_user = SimpleNamespace(id=20)
    msg.reply_to_message = SimpleNamespace(message_id=reply_to) if reply_to else None
    msg.markups = []

    async def answer(text, reply_markup=None, **_kw):
        msg.sent.append(text)
        msg.markups.append(reply_markup)
        return SimpleNamespace(message_id=777)
    msg.answer = answer
    return msg


class BrandStepTest(unittest.IsolatedAsyncioTestCase):

    async def test_category_prompt_starts_waiting_for_brands(self):
        ex = BrandExecutor({"type_code": "", "type_name": "", "tms": []})
        cb = FakeCallback("photo:abcd1234:any")
        cb.message.chat = SimpleNamespace(id=10)
        cb.message.message_id = 555
        cb.from_user = SimpleNamespace(id=20)
        await handlers.photo_choice(cb, orchestrator(ex))
        self.assertEqual(ex.waits[(10, 20)], ("abcd1234", 555))

    async def test_brands_are_taken_and_categories_shown_again(self):
        ex = BrandExecutor({"type_code": "", "type_name": "", "tms": []},
                           picked=["Peli"], unknown=["нетакой"])
        ex.waits[(10, 20)] = ("abcd1234", 555)
        msg = text_message("Peli, нетакой")
        self.assertTrue(await handlers._photo_brands(msg, orchestrator(ex)))
        self.assertIn("Марки: Peli.", msg.sent[0])
        self.assertIn("«нетакой»", msg.sent[0])
        self.assertIsNotNone(msg.markups[0], "кнопки категорий рядом")
        self.assertEqual(ex.waits[(10, 20)], ("abcd1234", 777), "ждём у нового сообщения")

    async def test_not_brands_goes_to_the_agent(self):
        ex = BrandExecutor({"type_code": "", "type_name": "", "tms": []})
        ex.waits[(10, 20)] = ("abcd1234", 555)
        msg = text_message("сколько стоит Classen Adventure")
        self.assertFalse(await handlers._photo_brands(msg, orchestrator(ex)))
        self.assertEqual(msg.sent, [])

    async def test_reply_with_unknown_brands_is_answered(self):
        """Прямой ответ на сообщение с кнопками — всегда про бренды: молчать нельзя."""
        ex = BrandExecutor({"type_code": "", "type_name": "", "tms": []}, unknown=["Ххх"])
        ex.waits[(10, 20)] = ("abcd1234", 555)
        msg = text_message("Ххх", reply_to=555)
        self.assertTrue(await handlers._photo_brands(msg, orchestrator(ex)))
        self.assertIn("Не нашёл", msg.sent[0])

    async def test_nobody_waiting(self):
        msg = text_message("Classen")
        self.assertFalse(await handlers._photo_brands(msg, orchestrator(BrandExecutor({}))))


class FakeStore:
    def __init__(self):
        self.values = {}

    async def get_setting(self, key, default=""):
        return self.values.get(key, default)

    async def set_setting(self, key, value):
        self.values[key] = value


class PhotoDaysTest(unittest.IsolatedAsyncioTestCase):

    async def run_cmd(self, args, store):
        msg = FakeMessage()
        await catalog_handlers.cmd_photo_days(msg, SimpleNamespace(args=args), store)
        return msg.sent[0]

    async def test_shows_default(self):
        self.assertIn("180 дн", await self.run_cmd("", FakeStore()))

    async def test_sets_value(self):
        store = FakeStore()
        answer = await self.run_cmd("90", store)
        self.assertEqual(store.values["photo_recent_days"], "90")
        self.assertIn("90 дн", answer)
        self.assertIn("было 180", answer)

    async def test_rejects_garbage_and_range(self):
        store = FakeStore()
        for bad in ("abc", "0", "5000"):
            self.assertIn("от 1 до", await self.run_cmd(bad, store))
        self.assertEqual(store.values, {})


if __name__ == "__main__":
    unittest.main()
