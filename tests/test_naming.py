"""Очистка имён от символов, ломающих XML выгрузки на сайт (§19.5)."""
import unittest

from src.price_tool.naming import FORBIDDEN, is_xml_safe, violations, xml_safe


class XmlSafeTest(unittest.TestCase):
    def test_ampersand_becomes_word(self):
        self.assertEqual(xml_safe("Quick Step & Co"), "Quick Step и Co")

    def test_no_double_spaces_after_replacement(self):
        self.assertEqual(xml_safe("Дуб &  Сосна"), "Дуб и Сосна")

    def test_angle_brackets_removed(self):
        self.assertEqual(xml_safe("Плитка <новинка>"), "Плитка новинка")

    def test_paired_quotes_become_typographic(self):
        self.assertEqual(xml_safe('Коллекция "Милан"'), "Коллекция «Милан»")

    def test_odd_quote_dropped(self):
        self.assertEqual(xml_safe('Дуб 12" светлый'), "Дуб 12 светлый")

    def test_apostrophe_survives(self):
        """L'Antic Colonial — законное имя; калечить его ради гипотезы нельзя."""
        self.assertEqual(xml_safe("L'Antic Colonial Bali"), "L'Antic Colonial Bali")
        self.assertTrue(is_xml_safe("L'Antic Colonial Bali"))

    def test_control_characters_removed(self):
        self.assertEqual(xml_safe("Дуб\x00 Мил\x1fас"), "Дуб Милас")

    def test_tab_and_newline_are_not_control(self):
        """Табуляция и перевод строки в XML допустимы — остаются, схлопываясь в пробел."""
        self.assertEqual(xml_safe("Дуб\tМилас"), "Дуб Милас")
        self.assertEqual(xml_safe("Дуб\nМилас"), "Дуб Милас")

    def test_clean_name_untouched(self):
        name = "CAMSAN Platinum Plus Дуб Милас 1380x190x10"
        self.assertEqual(xml_safe(name), name)
        self.assertTrue(is_xml_safe(name))

    def test_empty_and_none(self):
        self.assertEqual(xml_safe(None), "")
        self.assertEqual(xml_safe(""), "")
        self.assertTrue(is_xml_safe(None))

    def test_result_is_always_safe(self):
        for raw in ('A & B', 'x <y> z', 'Он сказал "да"', "\x01\x02", 'a"b"c"d'):
            with self.subTest(raw=raw):
                self.assertTrue(is_xml_safe(xml_safe(raw)), raw)


class ViolationsTest(unittest.TestCase):
    def test_lists_offending_characters(self):
        self.assertEqual(violations("A & B < C"), ["&", "<"])

    def test_control_named_in_words(self):
        self.assertIn("управляющие символы", violations("Дуб\x00"))

    def test_clean_name_has_none(self):
        self.assertEqual(violations("Дуб Милас"), [])

    def test_apostrophe_is_not_a_violation(self):
        self.assertEqual(violations("L'Antic"), [])

    def test_forbidden_set_matches_documented_rule(self):
        self.assertEqual(set(FORBIDDEN), {"&", "<", ">", '"'})


if __name__ == "__main__":
    unittest.main()
