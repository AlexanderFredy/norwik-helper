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
