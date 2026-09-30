"""Список получателей дайджеста по фото и команды управления им.

Доступ к боту и подписка на рассылку — РАЗНЫЕ вещи: пользоваться ботом нужно всем
менеджерам, а получать по понедельникам сводку про фото — тем, кто этим занимается.
Админ здесь такой же участник: нет его в списке — дайджест ему не приходит.
"""
import tempfile
import unittest
from pathlib import Path

from src.bot import catalog_handlers as ch
from src.storage.photo_subscribers import PhotoSubscriberStore
from src.storage.users import AllowedUser


class FakeMessage:
    def __init__(self, user_id=1):
        self.from_user = type("U", (), {"id": user_id})()
        self.sent: list[str] = []

    async def answer(self, text, reply_markup=None):
        self.sent.append(text)
        return self

    @property
    def text(self) -> str:
        return "\n".join(self.sent)


class Args:
    def __init__(self, args=""):
        self.args = args


class FakeUsers:
    """Белый список доступа к боту."""

    def __init__(self, ids=()):
        self._ids = list(ids)

    async def list_all(self):
        return [AllowedUser(telegram_id=i, name=f"user{i}", added_by=1, added_at="")
                for i in self._ids]


class StoreTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.subs = PhotoSubscriberStore(Path(self._dir.name) / "t.db")
        await self.subs.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_add_and_list(self):
        self.assertTrue(await self.subs.add(100, "Иван", added_by=1))
        rows = await self.subs.list_all()
        self.assertEqual([(r.telegram_id, r.name) for r in rows], [(100, "Иван")])

    async def test_second_add_is_not_a_duplicate(self):
        await self.subs.add(100, "Иван")
        self.assertFalse(await self.subs.add(100, "Иван Петров"))
        rows = await self.subs.list_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "Иван Петров", "имя обновилось")

    async def test_added_at_survives_rename(self):
        """Дата отвечает на вопрос «с каких пор человек получает рассылку»."""
        await self.subs.add(100, "Иван")
        was = (await self.subs.list_all())[0].added_at
        await self.subs.add(100, "Иван Петров")
        self.assertEqual((await self.subs.list_all())[0].added_at, was)

    async def test_remove(self):
        await self.subs.add(100)
        self.assertTrue(await self.subs.remove(100))
        self.assertFalse(await self.subs.remove(100))
        self.assertEqual(await self.subs.list_all(), [])

    async def test_has(self):
        await self.subs.add(100)
        self.assertTrue(await self.subs.has(100))
        self.assertFalse(await self.subs.has(200))


class SummaryCommandTest(unittest.IsolatedAsyncioTestCase):
    """`/no_photo_summury` — ДЛЯ ВСЕХ, кто работает с агентом: фото добавляют менеджеры,
    им и нужно видеть, сколько работы осталось."""

    async def asyncSetUp(self):
        from src.storage.photo_watch import PhotoWatchStore

        self._dir = tempfile.TemporaryDirectory()
        self.watch = PhotoWatchStore(Path(self._dir.name) / "w.db")
        await self.watch.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def seen(self, tm="Classen", created="2026-08-01", state=None):
        from src.website_tool import photos

        await self.watch.observe([{"ref": tm, "site_id": "1", "tm": tm,
                                   "collection": "Manor", "name": "Вернон",
                                   "created": created,
                                   "state": state or photos.NONE}], today="2026-09-28")

    async def run_cmd(self, args=""):
        msg = FakeMessage()
        await ch.cmd_no_photo_summary(msg, Args(args), self.watch)
        return msg

    async def test_anyone_gets_the_summary(self):
        """Никакой проверки на админа: доступ уже ограничен белым списком."""
        await self.seen()
        msg = await self.run_cmd()
        self.assertIn("Всего ждут фото: 1 поз.", msg.text)
        self.assertIn("- Classen — 1", msg.text)

    async def test_mark_can_be_named(self):
        await self.seen(tm="Classen")
        await self.seen(tm="Peli")
        msg = await self.run_cmd("peli")
        self.assertIn("- Peli — 1", msg.text)
        self.assertNotIn("Classen", msg.text)

    async def test_empty_journal_is_not_reported_as_order(self):
        """«Ждут 0» при ненаполненном журнале читается как «всё в порядке»."""
        msg = await self.run_cmd()
        self.assertIn("Журнал пуст", msg.text)

    async def test_nobody_waiting(self):
        from src.website_tool import photos

        await self.seen(state=photos.HAS)
        self.assertIn("не ждёт фото", (await self.run_cmd()).text)

    async def test_without_the_journal_store(self):
        msg = FakeMessage()
        await ch.cmd_no_photo_summary(msg, Args(""), None)
        self.assertIn("не подключён", msg.text)


class CommandTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.subs = PhotoSubscriberStore(Path(self._dir.name) / "t.db")
        await self.subs.init()
        self.users = FakeUsers([100, 200])

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def add(self, args, user_id=1):
        msg = FakeMessage(user_id)
        await ch.cmd_photo_sub_add(msg, Args(args), self.subs, self.users, is_admin=True)
        return msg

    async def test_empty_list_says_nobody_gets_it(self):
        msg = FakeMessage()
        await ch.cmd_photo_subs(msg, self.subs, self.users, is_admin=True)
        self.assertIn("не получает никто", msg.text)
        self.assertIn("в том числе администратору", msg.text)

    async def test_add_and_show(self):
        await self.add("100 Иван")
        msg = FakeMessage()
        await ch.cmd_photo_subs(msg, self.subs, self.users, is_admin=True)
        self.assertIn("1. Иван (100)", msg.text)

    async def test_cannot_subscribe_someone_without_access(self):
        """Рассылка несёт ссылки на карточки каталога: отправить их человеку, которому
        пользоваться ботом не разрешали, — значит обойти белый список."""
        msg = await self.add("999")
        self.assertIn("нет доступа к боту", msg.text)
        self.assertIn("/adduser 999", msg.text)
        self.assertEqual(await self.subs.list_all(), [])

    async def test_admin_can_always_subscribe_himself(self):
        """`ADMIN_TELEGRAM_ID` проходит авторизацию МИМО белого списка, и в нём админа
        обычно нет вовсе: боевой случай 30.09.2026 — id 552457947 авторизован как админ,
        а в списке доступа его не было. Без этой ветки команда «подпиши меня» отвечала бы
        «у вас нет доступа к боту» тому, кто ботом и распоряжается."""
        msg = await self.add("777", user_id=777)
        self.assertIn("Подписан", msg.text)
        self.assertEqual([r.telegram_id for r in await self.subs.list_all()], [777])

    async def test_admin_still_cannot_subscribe_a_stranger(self):
        msg = await self.add("999", user_id=777)
        self.assertIn("нет доступа к боту", msg.text)

    async def test_name_falls_back_to_the_access_list(self):
        await self.add("200")
        self.assertEqual((await self.subs.list_all())[0].name, "user200")

    async def test_add_without_id(self):
        msg = await self.add("Иван")
        self.assertIn("Укажите telegram-id", msg.text)

    async def test_second_add_is_reported_as_such(self):
        await self.add("100 Иван")
        msg = await self.add("100 Иван")
        self.assertIn("Уже был подписан", msg.text)

    async def test_delete_by_number(self):
        await self.add("100 Иван")
        await self.add("200 Пётр")
        msg = FakeMessage()
        await ch.cmd_photo_sub_delete(msg, Args("1"), self.subs, is_admin=True)
        self.assertIn("Отписан: Иван", msg.text)
        self.assertEqual([r.telegram_id for r in await self.subs.list_all()], [200])

    async def test_delete_by_id(self):
        """Номер удобнее, но id виден в том же списке — заставлять считать строки незачем."""
        await self.add("100 Иван")
        await self.add("200 Пётр")
        msg = FakeMessage()
        await ch.cmd_photo_sub_delete(msg, Args("200"), self.subs, is_admin=True)
        self.assertEqual([r.telegram_id for r in await self.subs.list_all()], [100])

    async def test_delete_nonsense(self):
        msg = FakeMessage()
        await ch.cmd_photo_sub_delete(msg, Args("нет"), self.subs, is_admin=True)
        self.assertIn("Нужен номер", msg.text)

    async def test_manager_cannot_manage_the_list(self):
        for call in (
            ch.cmd_photo_subs(FakeMessage(), self.subs, self.users, is_admin=False),
        ):
            await call
        msg = FakeMessage()
        await ch.cmd_photo_sub_add(msg, Args("100"), self.subs, self.users,
                                   is_admin=False)
        self.assertIn("только администратору", msg.text)
        self.assertEqual(await self.subs.list_all(), [])


if __name__ == "__main__":
    unittest.main()
