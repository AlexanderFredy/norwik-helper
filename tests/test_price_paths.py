"""Пути к прайсам при переезде базы с Windows на Linux (08.10.2026).

База хранит «data\\prices\\x.xlsx». На Linux обратный слэш — обычный символ имени: путь не
указывает ни на что, а уборка сирот при первом старте стёрла бы ВСЕ прайсы.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.storage import path_fix, price_files


class HelpersTest(unittest.TestCase):

    def test_text_always_uses_forward_slashes(self):
        self.assertEqual(price_files.text("data\\prices\\203e1c440c510890.xlsx"),
                         "data/prices/203e1c440c510890.xlsx")
        self.assertEqual(price_files.text(Path("data") / "prices" / "a.xls"), "data/prices/a.xls")
        self.assertEqual(price_files.text(None), "")

    def test_to_path_reads_an_old_windows_record(self):
        """Ровно то, что делает Linux: путь разбирается по прямым слэшам."""
        self.assertEqual(price_files.to_path("data\\prices\\x.xlsx").parts,
                         ("data", "prices", "x.xlsx"))


class FilesTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "users.db"
        self.saved = price_files.save(self.db, "Прайс.xlsx", b"PK content")

    def tearDown(self):
        self._dir.cleanup()

    def test_a_windows_style_record_is_loaded(self):
        windows = str(self.saved).replace("/", "\\")
        self.assertEqual(price_files.load(windows), b"PK content")

    def test_sweep_keeps_a_file_referenced_with_backslashes(self):
        windows = str(self.saved).replace("/", "\\")
        self.assertEqual(price_files.sweep(self.db, {windows}), 0)
        self.assertTrue(self.saved.is_file())

    def test_sweep_refuses_when_no_reference_points_to_a_file(self):
        """ПРЕДОХРАНИТЕЛЬ: ссылки есть, но ни одна не указывает на файл — значит пути не
        того вида, а не «все файлы сироты». Стирать каталог в такой момент нельзя."""
        self.assertEqual(price_files.sweep(self.db, {"/elsewhere/prices/nope.xlsx"}), 0)
        self.assertTrue(self.saved.is_file())

    def test_sweep_still_removes_a_real_orphan(self):
        orphan = price_files.save(self.db, "сирота.xlsx", b"other content")
        self.assertEqual(price_files.sweep(self.db, {str(self.saved)}), 1)
        self.assertFalse(orphan.is_file())
        self.assertTrue(self.saved.is_file())


class NormalizeTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "t.db"
        con = sqlite3.connect(self.db)
        con.executescript("""
            CREATE TABLE price (id INTEGER PRIMARY KEY, file_path TEXT NOT NULL);
            CREATE TABLE supplier_price_file (id INTEGER PRIMARY KEY,
                                              path TEXT NOT NULL UNIQUE);
            CREATE TABLE deferred_tasks (id INTEGER PRIMARY KEY, file_path TEXT);
        """)
        con.execute("INSERT INTO price (file_path) VALUES ('data\\prices\\a.xlsx')")
        con.execute("INSERT INTO supplier_price_file (path) VALUES ('data\\prices\\a.xlsx')")
        # тот же файл уже записан и прямыми слэшами — UNIQUE не должен уронить старт
        con.execute("INSERT INTO supplier_price_file (path) VALUES ('data/prices/b.xls')")
        con.execute("INSERT INTO supplier_price_file (path) VALUES ('data\\prices\\b.xls')")
        con.execute("INSERT INTO deferred_tasks (file_path) VALUES (NULL)")
        con.commit()
        con.close()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_paths_become_forward_slashed(self):
        fixed = await path_fix.normalize(self.db)
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT file_path FROM price").fetchone()[0],
                         "data/prices/a.xlsx")
        paths = sorted(r[0] for r in con.execute("SELECT path FROM supplier_price_file"))
        con.close()
        self.assertIn("data/prices/a.xlsx", paths)
        self.assertIn("data/prices/b.xls", paths)
        self.assertEqual(fixed, 2, "дубль под UNIQUE пропущен, а не уронил старт")

    async def test_it_is_idempotent_and_skips_missing_tables(self):
        await path_fix.normalize(self.db)
        self.assertEqual(await path_fix.normalize(self.db), 0)


if __name__ == "__main__":
    unittest.main()
