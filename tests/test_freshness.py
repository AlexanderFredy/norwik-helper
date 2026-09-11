"""Какой прайс свежее, уведомление о новом и уборка сирот (§9.8).

Дата из прайса — не единственный источник и не всегда надёжный: у Most Floor её в шапке
нет вовсе, а переэкспорт со старой шапкой обычное дело. Поэтому даты сравниваются по
очереди: дата прайса, потом дата получения (для почты — дата письма).
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.bot import pricing_handlers as ph
from src.price_tool.freshness import date_from_name, human_date, is_newer
from src.storage import price_files
from src.storage.pricing import PricingStore

XLSX = b"PK\x03\x04 fake"


class DateFromNameTest(unittest.TestCase):
    """Считает КОД: очередь наполняется раньше, чем модель увидит файл."""

    def test_day_month_year(self):
        self.assertEqual(date_from_name("Прайс-лист Grandeco (31.08.2026).XLSX"),
                         "2026-08-31")

    def test_iso(self):
        self.assertEqual(date_from_name("price 2026-09-01.csv"), "2026-09-01")

    def test_short_year(self):
        self.assertEqual(date_from_name("Прайс 01.09.26.xlsx"), "2026-09-01")

    def test_day_and_month_only(self):
        today = date.today()
        self.assertEqual(date_from_name(f"Прайс с {today.day:02}.{today.month:02}.xlsx"),
                         today.isoformat())

    def test_future_date_belongs_to_the_previous_year(self):
        """«Прайс с 20.12», присланный в январе, — это прошлый декабрь."""
        ahead = date.today() + timedelta(days=200)
        got = date_from_name(f"Прайс {ahead.day:02}.{ahead.month:02}.xlsx")
        self.assertEqual(got, ahead.replace(year=ahead.year - 1).isoformat())

    def test_no_date_at_all(self):
        self.assertIsNone(date_from_name("Прайс лист Most Floor.xlsx"))

    def test_nonsense_date_is_not_invented(self):
        self.assertIsNone(date_from_name("Прайс 45.99.xlsx"))


class IsNewerTest(unittest.TestCase):

    def test_price_date_decides_first(self):
        self.assertTrue(is_newer({"price_date": "2026-09-15"},
                                 {"price_date": "2026-09-01"}))
        self.assertFalse(is_newer({"price_date": "2026-08-01"},
                                  {"price_date": "2026-09-01"}))

    def test_received_date_decides_when_price_dates_are_equal(self):
        """Переэкспорт со старой шапкой: даты прайса те же, письмо пришло позже."""
        same = "2026-09-01"
        self.assertTrue(is_newer({"price_date": same, "received_at": "2026-09-11T10:00"},
                                 {"price_date": same, "received_at": "2026-09-05T10:00"}))

    def test_received_date_decides_when_price_dates_are_unknown(self):
        self.assertFalse(is_newer({"received_at": "2026-09-01T10:00"},
                                  {"received_at": "2026-09-10T10:00"}))

    def test_full_ignorance_lets_the_newcomer_win(self):
        """Иначе повторная присылка файла ничего бы не обновляла — а ею и возвращаются."""
        self.assertTrue(is_newer({}, {}))

    def test_human_date_falls_back_to_receipt(self):
        self.assertEqual(human_date({"price_date": "2026-09-15"}), "15.09.26")
        self.assertEqual(human_date({"received_at": "2026-09-15T10:00:00"}), "15.09.26")
        self.assertEqual(human_date({}), "")


class QueueFreshnessTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        ph._files.clear()

    async def asyncTearDown(self):
        ph._files.clear()
        self._dir.cleanup()

    async def test_older_version_does_not_displace_the_newer(self):
        await ph._enqueue(self.store, 42, "Прайс Монарх 15.09.2026.xlsx", XLSX)
        text = await ph._enqueue(self.store, 42, "Прайс Монарх 01.09.2026.xlsx",
                                 XLSX + b" old")
        self.assertIn("старее", text)
        queue = await self.store.list_queue(42)
        self.assertEqual(queue[0]["filename"], "Прайс Монарх 15.09.2026.xlsx")

    async def test_newer_version_displaces_the_older(self):
        await ph._enqueue(self.store, 42, "Прайс Монарх 01.09.2026.xlsx", XLSX)
        text = await ph._enqueue(self.store, 42, "Прайс Монарх 15.09.2026.xlsx",
                                 XLSX + b" new")
        self.assertIn("заменил", text)
        queue = await self.store.list_queue(42)
        self.assertEqual(queue[0]["price_date"], "2026-09-15")


class NoticeTest(unittest.IsolatedAsyncioTestCase):
    """«ПОЯВИЛСЯ НОВЫЙ ПРАЙС» — плашка о том, что разбираемое уже устарело."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        ph._files.clear()

    async def asyncTearDown(self):
        ph._files.clear()
        self._dir.cleanup()

    async def test_new_version_of_the_active_price_is_announced(self):
        await ph._remember_file(self.store, 42, "Прайс Монарх 01.09.2026.xlsx", XLSX)
        await ph._enqueue(self.store, 42, "Прайс Монарх 15.09.2026.xlsx", XLSX)
        notice = await ph._fresher_notice(self.store, 42)
        self.assertIn("ПОЯВИЛСЯ НОВЫЙ ПРАЙС", notice)
        self.assertIn("15.09.26", notice)

    async def test_other_supplier_is_not_announced(self):
        await ph._remember_file(self.store, 42, "Прайс Монарх 01.09.2026.xlsx", XLSX)
        await ph._enqueue(self.store, 42, "Линдервуд.pdf", b"%PDF fake")
        self.assertEqual(await ph._fresher_notice(self.store, 42), "")

    async def test_same_supplier_other_format_is_announced(self):
        """Имя поставщика из плана прогона встречается в имени нового файла."""
        await ph._remember_file(self.store, 42, "Монарх ламинат.xlsx", XLSX)
        await self.store.start_run(42, "Монарх", "Монарх ламинат.xlsx", [])
        await ph._enqueue(self.store, 42, "Монарх плитка 15.09.2026.pdf", b"%PDF fake")
        self.assertIn("ПОЯВИЛСЯ НОВЫЙ ПРАЙС", await ph._fresher_notice(self.store, 42))

    async def test_nothing_in_work_means_no_notice(self):
        await ph._enqueue(self.store, 42, "Прайс.xlsx", XLSX)
        self.assertEqual(await ph._fresher_notice(self.store, 42), "")

    async def test_notice_is_bold_and_hint_is_italic(self):
        text, entities = ph._with_hint("Предложение", hint="подсказка",
                                       notice="ПОЯВИЛСЯ НОВЫЙ ПРАЙС")
        styles = {e.type for e in entities}
        self.assertEqual(styles, {"bold", "italic"})
        bold = next(e for e in entities if e.type == "bold")
        cut = text.encode("utf-16-le")[bold.offset * 2:
                                       (bold.offset + bold.length) * 2].decode("utf-16-le")
        self.assertEqual(cut, "ПОЯВИЛСЯ НОВЫЙ ПРАЙС")


class SweepTest(unittest.IsolatedAsyncioTestCase):
    """Уборка сирот при старте: файл пишется раньше строки в базе."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        ph._files.clear()

    async def asyncTearDown(self):
        ph._files.clear()
        self._dir.cleanup()

    async def test_orphan_is_removed_and_the_rest_kept(self):
        await ph._remember_file(self.store, 42, "В работе.xlsx", XLSX)
        kept = (await self.store.get_active_price(42))["path"]
        orphan = price_files.save(self.store.db_path, "Сирота.xlsx", b"PK orphan")

        removed = price_files.sweep(self.store.db_path,
                                    await self.store.known_price_paths())
        self.assertEqual(removed, 1)
        self.assertFalse(Path(orphan).is_file())
        self.assertTrue(Path(kept).is_file())

    async def test_queued_file_is_not_an_orphan(self):
        await ph._enqueue(self.store, 42, "Ждёт.xlsx", XLSX)
        path = (await self.store.list_queue(42))[0]["path"]
        price_files.sweep(self.store.db_path, await self.store.known_price_paths())
        self.assertTrue(Path(path).is_file())

    async def test_empty_folder_is_fine(self):
        self.assertEqual(price_files.sweep(self.store.db_path, set()), 0)


if __name__ == "__main__":
    unittest.main()
