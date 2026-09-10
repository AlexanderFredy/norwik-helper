"""Активный прайс переживает перезапуск бота (§9.7).

Случай из жизни, 10.09.2026. Оборвалась связь с моделью, бот перезапустили, админ написал
«продолжай дальше» — и получил ответ менеджерского агента «не вижу, какой товар искать».
История диалога лежала в базе и рестарт пережила, а файл прайса жил только в памяти
процесса: пара разорвалась, и текст перестал считаться ответом по прайсу.
"""
import tempfile
import unittest
from pathlib import Path

from src.bot import pricing_handlers as ph
from src.storage import price_files
from src.storage.pricing import PricingStore

CONTENT = b"price;body;\n1;2;\n"


class ActivePriceTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        ph._files.clear()

    async def asyncTearDown(self):
        ph._files.clear()
        self._dir.cleanup()

    async def test_file_survives_a_restart(self):
        await ph._remember_file(self.store, 42, "Most Floor.xlsx", CONTENT)

        ph._files.clear()                       # это и есть перезапуск процесса
        self.assertNotIn(42, ph._files)

        restored = await ph.restore_active_prices(self.store)
        self.assertEqual(restored, 1)
        self.assertEqual(ph._files[42], ("Most Floor.xlsx", CONTENT))

    async def test_text_is_routed_to_the_price_dialogue_again(self):
        """Ради этого всё и делается: фильтр ответа смотрит именно в `_files`."""
        await ph._remember_file(self.store, 42, "Most Floor.xlsx", CONTENT)
        ph._files.clear()
        await ph.restore_active_prices(self.store)

        message = type("M", (), {"from_user": type("U", (), {"id": 42})(),
                                 "text": "продолжай дальше"})()
        self.assertTrue(ph._is_price_reply(message))

    async def test_forgetting_removes_the_file(self):
        await ph._remember_file(self.store, 42, "Most Floor.xlsx", CONTENT)
        path = (await self.store.get_active_price(42))["path"]
        self.assertTrue(Path(path).is_file())

        await ph._forget_file(self.store, 42)
        self.assertNotIn(42, ph._files)
        self.assertIsNone(await self.store.get_active_price(42))
        self.assertFalse(Path(path).is_file())

    async def test_file_of_a_deferred_task_is_kept(self):
        """Файл под отложенной задачей стирать нельзя — вернуться к ней будет не с чем."""
        await ph._remember_file(self.store, 42, "Most Floor.xlsx", CONTENT)
        path = (await self.store.get_active_price(42))["path"]
        await self.store.defer_task(42, {
            "supplier": "Most Floor", "price_doc": "Most Floor.xlsx",
            "tm_code": "T1", "tm_name": "Most Flooring", "file_path": path})

        await ph._forget_file(self.store, 42)
        self.assertIsNone(await self.store.get_active_price(42))
        self.assertTrue(Path(path).is_file(), "прайс отложенной задачи удалён")

    async def test_missing_file_clears_the_row(self):
        """Файл исчез с диска — строку не тащим: она обещала бы то, чего нет."""
        await ph._remember_file(self.store, 42, "Most Floor.xlsx", CONTENT)
        price_files.forget([(await self.store.get_active_price(42))["path"]])
        ph._files.clear()

        self.assertEqual(await ph.restore_active_prices(self.store), 0)
        self.assertIsNone(await self.store.get_active_price(42))

    async def test_new_price_replaces_the_previous_one(self):
        await ph._remember_file(self.store, 42, "Первый.xlsx", CONTENT)
        await ph._remember_file(self.store, 42, "Второй.xlsx", b"other;body;\n")
        row = await self.store.get_active_price(42)
        self.assertEqual(row["filename"], "Второй.xlsx")
        self.assertEqual(ph._files[42][0], "Второй.xlsx")


if __name__ == "__main__":
    unittest.main()
