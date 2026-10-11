"""Нулевая часть размера в наименовании: «1500x750x0» → «1500x750» (просьба админа 11.10.2026).

Ноль приходит из 1С: размер для имени она собирает из полей, и незаполненная толщина даёт
«x0». Такой хвост — не размер, а пустое поле.
"""
import unittest

from src.price_tool.items import build_name, site_name
from src.price_tool.naming import (collection_from_folder, drop_zero_size, folder_name,
                                   format_size)


class DropZeroTest(unittest.TestCase):

    def test_zero_thickness_is_dropped(self):
        self.assertEqual(drop_zero_size("1500x750x0"), "1500x750")
        self.assertEqual(drop_zero_size("60х60х0,0"), "60х60")      # кириллическая «х»
        self.assertEqual(drop_zero_size("1500*750*0"), "1500*750")

    def test_real_sizes_stay(self):
        for text in ("1500x750x10", "1200x600x9", "60x60", "120x60x0,9", "1290x190x0.5",
                     "300x600x05"):
            self.assertEqual(drop_zero_size(text), text, text)

    def test_empty(self):
        self.assertEqual(drop_zero_size(None), "")
        self.assertEqual(drop_zero_size(""), "")


class NameTest(unittest.TestCase):

    def test_name_from_a_1c_size_with_zero_thickness(self):
        name = build_name("Керамогранит", "Atlas Concorde / Атлас конкорд", "Boost",
                          "Grey", "1500x750x0")
        self.assertTrue(name.endswith("1500x750"), name)
        self.assertNotIn("x0", name)

    def test_site_name_too(self):
        self.assertEqual(site_name("Grey", "1500x750x0"), "Grey 1500x750")

    def test_format_size_treats_zero_as_absent(self):
        """Незаполненное числовое поле 1С приходит нулём."""
        self.assertEqual(format_size(1500, 750, 0), "1500x750")
        self.assertEqual(format_size(1500, 750, 0.0), "1500x750")
        self.assertEqual(format_size(1500, 0, 9), "", "половина размера бессмысленна")
        self.assertEqual(format_size(1200, 600, 9), "1200x600x9")


class FolderTest(unittest.TestCase):

    def test_new_folder_names_have_no_zero(self):
        self.assertEqual(folder_name("Boost", "1500x750x0"), "Boost 1500x750")

    def test_old_folder_with_zero_is_still_recognised(self):
        """Папки, заведённые до правки, могли получить «x0» — коллекция из них выделяется."""
        self.assertEqual(collection_from_folder("Boost 1500x750x0", "1500x750x0"), "Boost")
        self.assertEqual(collection_from_folder("Boost 1500x750", "1500x750x0"), "Boost")


if __name__ == "__main__":
    unittest.main()
