import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.storage.users import UserStore


class UserStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = UserStore(Path(self._tmp.name) / "users.db")
        asyncio.run(self.store.init())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_add_and_check(self) -> None:
        async def run() -> None:
            self.assertFalse(await self.store.is_allowed(100))
            self.assertTrue(await self.store.add(100, added_by=1, name="Иван"))
            self.assertTrue(await self.store.is_allowed(100))

        asyncio.run(run())

    def test_add_duplicate(self) -> None:
        async def run() -> None:
            self.assertTrue(await self.store.add(100, added_by=1))
            self.assertFalse(await self.store.add(100, added_by=1))

        asyncio.run(run())

    def test_remove(self) -> None:
        async def run() -> None:
            await self.store.add(100, added_by=1)
            self.assertTrue(await self.store.remove(100))
            self.assertFalse(await self.store.remove(100))
            self.assertFalse(await self.store.is_allowed(100))

        asyncio.run(run())

    def test_list_all(self) -> None:
        async def run() -> None:
            await self.store.add(100, added_by=1, name="Иван")
            await self.store.add(200, added_by=1)
            users = await self.store.list_all()
            self.assertEqual([u.telegram_id for u in users], [100, 200])
            self.assertEqual(users[0].name, "Иван")
            self.assertIsNone(users[1].name)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
