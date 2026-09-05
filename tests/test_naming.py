"""Символы, опасные для XML-выгрузки на сайт (§19.5).

Две разные категории, и тест на то, что мы их не путаем: управляющие символы удаляем
безусловно, символы разметки НЕ ТРОГАЕМ — их дело экранировать на стороне выгрузки.
"""
import unittest

from src.price_tool.naming import (MARKUP, is_xml_safe, markup_chars, violations,
                                   xml_safe)


class ControlCharsTest(unittest.TestCase):
    """Их не спасает экранирование — документ с ними невалиден в принципе."""

    def test_control_characters_removed(self):
        self.assertEqual(xml_safe("Дуб\x00 Мил\x1fас"), "Дуб Милас")

    def test_control_characters_are_a_violation(self):
        self.assertIn("управляющие символы", violations("Дуб\x00"))
        self.assertFalse(is_xml_safe("Дуб\x00"))

    def test_tab_and_newline_are_legal_but_collapsed(self):
        """В XML допустимы, но в наименовании товара им делать нечего."""
        self.assertEqual(xml_safe("Дуб\tМилас"), "Дуб Милас")
        self.assertEqual(xml_safe("Floorwood Genesis\n SPC"), "Floorwood Genesis SPC")
        self.assertTrue(is_xml_safe("Дуб\tМилас"))


class MarkupCharsTest(unittest.TestCase):
    """Законные символы имени. Портить их нельзя — 414 позиций в базе."""

    def test_ampersand_survives_untouched(self):
        self.assertEqual(xml_safe("Onyx&More"), "Onyx&More")
        self.assertTrue(is_xml_safe("Onyx&More"))

    def test_quotes_and_apostrophe_survive(self):
        for name in ('Elemento Ad "L" Old Chicago', "L'Antic Colonial", "CEPPO DI GRE'"):
            with self.subTest(name=name):
                self.assertEqual(xml_safe(name), name)
                self.assertTrue(is_xml_safe(name))

    def test_angle_brackets_survive(self):
        self.assertEqual(xml_safe("Плитка <новинка>"), "Плитка <новинка>")

    def test_markup_chars_are_reported_not_removed(self):
        self.assertEqual(markup_chars("Onyx&More"), ["&"])
        self.assertEqual(markup_chars('A & B < C "D"'), ["&", "<", '"'])

    def test_markup_is_not_a_violation(self):
        self.assertEqual(violations("Onyx&More"), [])

    def test_clean_name_has_no_markup(self):
        self.assertEqual(markup_chars("CAMSAN Platinum Plus Дуб Милас"), [])

    def test_markup_set_is_the_five_xml_specials(self):
        self.assertEqual(set(MARKUP), {"&", "<", ">", '"', "'"})


class EdgeCasesTest(unittest.TestCase):
    def test_empty_and_none(self):
        self.assertEqual(xml_safe(None), "")
        self.assertEqual(xml_safe(""), "")
        self.assertTrue(is_xml_safe(None))
        self.assertEqual(markup_chars(None), [])

    def test_clean_name_untouched(self):
        name = "CAMSAN Platinum Plus Дуб Милас 1380x190x10"
        self.assertEqual(xml_safe(name), name)

    def test_result_is_always_safe(self):
        for raw in ("A & B", "x <y> z", 'Он сказал "да"', "\x01\x02", "a\x00b"):
            with self.subTest(raw=raw):
                self.assertTrue(is_xml_safe(xml_safe(raw)), raw)


if __name__ == "__main__":
    unittest.main()
