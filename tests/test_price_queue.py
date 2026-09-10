"""Очередь прайсов и вечный контекст разбора (§9.8).

Прежде новый файл ВЫТЕСНЯЛ текущий: `handle_price_document` затирал память и сбрасывал
диалог, и недоразобранный прайс исчезал молча. А контекст диалога протухал через час, унося
вместе с собой неотвеченный вопрос агента.

Теперь: пока прайс в работе, диалог не стареет; новые прайсы встают в очередь; новая версия
уже стоящего в очереди прайса заменяет старую.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.bot import pricing_handlers as ph
from src.storage.pricing import PricingStore

XLSX = b"PK\x03\x04 fake"          # содержимое неважно: ключ считается и без разбора


class QueueTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        ph._files.clear()

    async def asyncTearDown(self):
        ph._files.clear()
        self._dir.cleanup()

    async def test_new_version_replaces_the_queued_one(self):
        """Ради этого очередь и различает прайсы по ключу, а не по имени файла."""
        first = await ph._enqueue(self.store, 42, "Прайс Most Floor 01.09.xlsx", XLSX)
        self.assertIn("поставлен в очередь", first)

        second = await ph._enqueue(self.store, 42, "Прайс Most Floor 15.09.xlsx", XLSX)
        self.assertIn("заменил в очереди прежнюю версию", second)

        queue = await self.store.list_queue(42)
        self.assertEqual([q["filename"] for q in queue], ["Прайс Most Floor 15.09.xlsx"])

    async def test_different_price_lists_both_wait(self):
        await ph._enqueue(self.store, 42, "Most Floor.xlsx", XLSX)
        await ph._enqueue(self.store, 42, "Линдервуд.pdf", b"%PDF-1.4 fake")
        self.assertEqual(len(await self.store.list_queue(42)), 2)

    async def test_order_is_first_in_first_out(self):
        await ph._enqueue(self.store, 42, "Первый.xlsx", XLSX)
        await ph._enqueue(self.store, 42, "Второй.pdf", b"%PDF fake")
        taken = await self.store.take_next_price(42)
        self.assertEqual(taken["filename"], "Первый.xlsx")
        self.assertEqual(len(await self.store.list_queue(42)), 1)

    async def test_replaced_version_does_not_leave_a_file_behind(self):
        await ph._enqueue(self.store, 42, "Прайс 01.09.xlsx", XLSX)
        old = (await self.store.list_queue(42))[0]["path"]
        await ph._enqueue(self.store, 42, "Прайс 15.09.xlsx", XLSX + b" v2")
        self.assertFalse(Path(old).is_file(), "файл прежней версии остался на диске")

    async def test_queue_holds_a_reference_to_its_file(self):
        """Файл из очереди нельзя стереть заодно с активным прайсом."""
        await ph._enqueue(self.store, 42, "Ждёт.xlsx", XLSX)
        path = (await self.store.list_queue(42))[0]["path"]
        self.assertTrue(await self.store.price_file_in_use(path))

    async def test_taken_price_leaves_the_queue(self):
        await ph._enqueue(self.store, 42, "Один.xlsx", XLSX)
        await self.store.take_next_price(42)
        self.assertEqual(await self.store.list_queue(42), [])
        self.assertIsNone(await self.store.take_next_price(42))


class DialogLifetimeTest(unittest.IsolatedAsyncioTestCase):
    """Контекст живёт, пока прайс в работе, — и стареет, когда работы нет."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _age_dialog(self, minutes: int) -> None:
        import aiosqlite
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        async with aiosqlite.connect(self.store._db_path) as db:
            await db.execute("UPDATE dialog_state SET updated_at = ?", (stamp,))
            await db.commit()

    async def test_context_survives_while_a_price_is_in_work(self):
        await self.store.save_messages(42, [{"role": "user", "content": "разбираем прайс"}])
        await self.store.set_active_price(42, "Most Floor.xlsx", "/tmp/x.xlsx")
        await self._age_dialog(600)                    # десять часов раздумий

        history = await self.store.load_messages(42)
        self.assertEqual(len(history), 1, "контекст потерян, пока прайс в работе")

    async def test_context_expires_when_nothing_is_in_work(self):
        await self.store.save_messages(42, [{"role": "user", "content": "старый разговор"}])
        await self._age_dialog(600)
        self.assertEqual(await self.store.load_messages(42), [])

    async def test_finishing_the_price_lets_the_context_expire(self):
        """Прайс обработан — контекст снова обычный, и через час его не станет."""
        await self.store.save_messages(42, [{"role": "user", "content": "разбираем"}])
        await self.store.set_active_price(42, "Most Floor.xlsx", "/tmp/x.xlsx")
        await self.store.clear_active_price(42)
        await self._age_dialog(600)
        self.assertEqual(await self.store.load_messages(42), [])


if __name__ == "__main__":
    unittest.main()
