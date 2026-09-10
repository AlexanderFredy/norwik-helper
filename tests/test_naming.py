"""Символы, опасные для XML-выгрузки на сайт (§19.5).

Две разные категории, и тест на то, что мы их не путаем: управляющие символы удаляем
безусловно, символы разметки НЕ ТРОГАЕМ — их дело экранировать на стороне выгрузки.
"""
import unittest

from src.price_tool.naming import (MARKUP, collection_case, drop_own_article,
                                   ensure_type_prefix,
                                   fix_article_in_name, fix_caps, fix_collection_in_name,
                                   is_xml_safe, markup_chars, strip_type_prefix, tidy,
                                   violations, xml_safe)


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


class CapsTest(unittest.TestCase):
    """CAPS LOCK в карточке — опечатка ввода, а не смысл (§19.5)."""

    def test_caps_word_becomes_capitalized(self):
        self.assertEqual(fix_caps("Дуб МЕДОВЫЙ"), "Дуб Медовый")

    def test_article_and_latin_survive(self):
        """Латиница остаётся: там прописные осмысленны, а артикул переписывать нельзя."""
        self.assertEqual(fix_caps("Peli Anatolia 8мм Дуб МЕДОВЫЙ AN DSG 908"),
                         "Peli Anatolia 8мм Дуб Медовый AN DSG 908")
        self.assertEqual(fix_caps("SPC LVT EIR UNILIN"), "SPC LVT EIR UNILIN")

    def test_short_abbreviations_survive(self):
        for abbr in ("ПВХ панель", "ЛДСП белая", "МДФ", "СПБ"):
            with self.subTest(abbr=abbr):
                self.assertEqual(fix_caps(abbr), abbr)

    def test_mixed_case_word_untouched(self):
        """«ДубМедовый» — чей-то стиль, а не CAPS LOCK; границы слов не угадываем."""
        self.assertEqual(fix_caps("ДубМедовый"), "ДубМедовый")

    def test_already_normal_untouched(self):
        self.assertEqual(fix_caps("Дуб Медовый"), "Дуб Медовый")

    def test_empty(self):
        self.assertEqual(fix_caps(None), "")
        self.assertEqual(fix_caps(""), "")


class TidyTest(unittest.TestCase):
    """Одна функция на всю молчаливую нормализацию (§19.5)."""

    def test_spaces_and_caps_together(self):
        self.assertEqual(tidy("Ламинат Peli  Дуб МЕДОВЫЙ  AN DSG 908"),
                         "Ламинат Peli Дуб Медовый AN DSG 908")

    def test_edges_trimmed(self):
        self.assertEqual(tidy("  Дуб Милас  "), "Дуб Милас")

    def test_control_chars_removed(self):
        self.assertEqual(tidy("Дуб" + chr(0) + " Милас"), "Дуб Милас")

    def test_markup_survives(self):
        """Onyx&More — настоящее имя коллекции, чистка его не касается."""
        self.assertEqual(tidy("Onyx&More"), "Onyx&More")

    def test_clean_name_untouched(self):
        name = "CAMSAN Platinum Plus Дуб Милас 1380x190x10"
        self.assertEqual(tidy(name), name)


class TypePrefixTest(unittest.TestCase):
    """Вид товара первым в наименовании и полном, но НЕ в наименовании для сайта (§19.5)."""

    def test_prefix_added_when_missing(self):
        self.assertEqual(
            ensure_type_prefix("Peli Anatolia Platinium Дуб Голд AN PLT 905", "Ламинат"),
            "Ламинат Peli Anatolia Platinium Дуб Голд AN PLT 905")

    def test_prefix_not_doubled(self):
        name = "Ламинат Peli Vintage Ван Браун VN-511"
        self.assertEqual(ensure_type_prefix(name, "Ламинат"), name)

    def test_case_insensitive_check(self):
        """«ЛАМИНАТ Peli …» уже начинается с вида товара — второй раз не ставим."""
        self.assertEqual(ensure_type_prefix("ЛАМИНАТ Peli Дуб", "Ламинат"),
                         "ЛАМИНАТ Peli Дуб")

    def test_product_type_with_trailing_space(self):
        """1С отдаёт вид товара с хвостовым пробелом — «Виниловый ламинат »."""
        self.assertEqual(ensure_type_prefix("Peli Дуб", "Виниловый ламинат "),
                         "Виниловый ламинат Peli Дуб")

    def test_empty_type_leaves_name(self):
        self.assertEqual(ensure_type_prefix("Peli Дуб", ""), "Peli Дуб")
        self.assertEqual(ensure_type_prefix("Peli Дуб", None), "Peli Дуб")

    def test_strip_for_site_name(self):
        self.assertEqual(strip_type_prefix("Ламинат Дуб Голд", "Ламинат"), "Дуб Голд")

    def test_strip_does_nothing_when_absent(self):
        self.assertEqual(strip_type_prefix("Дуб Голд", "Ламинат"), "Дуб Голд")

    def test_round_trip(self):
        site = "Дуб Голд"
        self.assertEqual(strip_type_prefix(ensure_type_prefix(site, "Ламинат"), "Ламинат"),
                         site)


class ArticleInNameTest(unittest.TestCase):
    """Чужой артикул в наименовании — опечатка: ключ это реквизит, а не подпись."""

    KNOWN = {"AN PLT 909", "AN PLT 910", "AN PLT 911", "AN PLT 912"}

    def test_foreign_article_replaced(self):
        self.assertEqual(
            fix_article_in_name("Ламинат Peli Platinium Дуб Сильвер AN PLT 911",
                                "AN PLT 910", self.KNOWN),
            "Ламинат Peli Platinium Дуб Сильвер AN PLT 910")

    def test_own_article_present_untouched(self):
        """Свой артикул на месте — лишний текст рядом это что-то другое, не наше дело."""
        name = "Ламинат Peli Дуб Сеньи AN PLT 911"
        self.assertEqual(fix_article_in_name(name, "AN PLT 911", self.KNOWN), name)

    def test_unknown_token_left_alone(self):
        """Артикул, который никому не принадлежит, молча менять нельзя."""
        name = "Ламинат Peli Дуб Сильвер AN PLT 999"
        self.assertEqual(fix_article_in_name(name, "AN PLT 910", self.KNOWN), name)

    def test_two_foreign_articles_is_ambiguous(self):
        name = "Ламинат Peli AN PLT 911 и AN PLT 912"
        self.assertEqual(fix_article_in_name(name, "AN PLT 910", self.KNOWN), name)

    def test_longer_article_wins_over_its_prefix(self):
        known = {"LE 263", "LE 2630"}
        self.assertEqual(fix_article_in_name("Ламинат Peli LE 2630", "LE 517", known),
                         "Ламинат Peli LE 517")

    def test_no_article_no_change(self):
        self.assertEqual(fix_article_in_name("Ламинат Peli Дуб", "AN PLT 910", self.KNOWN),
                         "Ламинат Peli Дуб")

    def test_empty_inputs(self):
        self.assertEqual(fix_article_in_name(None, "AN PLT 910", self.KNOWN), "")
        self.assertEqual(fix_article_in_name("Дуб", None, self.KNOWN), "Дуб")


class LegacyTypeWordTest(unittest.TestCase):
    """«Водостойкий» — архаизм; вид товара в справочнике «Виниловый ламинат» (§19.5)."""

    def test_legacy_word_replaced_not_prepended(self):
        self.assertEqual(
            ensure_type_prefix("Водостойкий ламинат Vinilam Дуб Брюссель 04018",
                               "Виниловый ламинат"),
            "Виниловый ламинат Vinilam Дуб Брюссель 04018")

    def test_second_legacy_variant(self):
        self.assertEqual(
            ensure_type_prefix("Влагостойкий ламинат AQUAFLOOR Дуб", "Виниловый ламинат"),
            "Виниловый ламинат AQUAFLOOR Дуб")

    def test_canonical_untouched(self):
        name = "Виниловый ламинат Peli SPC Адана LQ-01"
        self.assertEqual(ensure_type_prefix(name, "Виниловый ламинат"), name)

    def test_legacy_word_of_other_type_not_touched(self):
        """«Водостойкий» не синоним «Ламината» — там дописываем, как обычно."""
        self.assertEqual(ensure_type_prefix("Водостойкий ламинат Peli", "Ламинат"),
                         "Ламинат Водостойкий ламинат Peli")


class CollectionCase(unittest.TestCase):
    """§19.10: в названии коллекции первая буква заглавная, остальные строчные."""

    def test_caps_latin(self):
        self.assertEqual(collection_case("QUARTZ"), "Quartz")

    def test_caps_cyrillic(self):
        self.assertEqual(collection_case("СТАРОДУБ"), "Стародуб")

    def test_lowercase_gets_capital(self):
        self.assertEqual(collection_case("quartz"), "Quartz")

    def test_already_canonical(self):
        self.assertEqual(collection_case("Linderwood"), "Linderwood")

    def test_two_words(self):
        self.assertEqual(collection_case("MOST FLOOR"), "Most Floor")

    def test_token_with_digits_untouched(self):
        """`AC5` и `8` — не слова: регистр там либо осмыслен, либо не при чём."""
        self.assertEqual(collection_case("GRUNWALD AC5 8"), "Grunwald AC5 8")

    def test_ampersand_name_untouched(self):
        """`Onyx&More` — настоящее имя коллекции; `Onyx&more` было бы порчей."""
        self.assertEqual(collection_case("Onyx&More"), "Onyx&More")

    def test_single_letter_untouched(self):
        self.assertEqual(collection_case("S QUARTZ"), "S Quartz")

    def test_spaces_and_control_chars(self):
        self.assertEqual(collection_case("  QUARTZ" + chr(0) + "  ADANA "), "Quartz Adana")

    def test_empty(self):
        self.assertEqual(collection_case(None), "")


class CollectionInName(unittest.TestCase):

    def test_renames_inside_item_name(self):
        self.assertEqual(
            fix_collection_in_name("Виниловый ламинат Linderwood QUARTZ Адана LQ-01",
                                   "QUARTZ"),
            "Виниловый ламинат Linderwood Quartz Адана LQ-01")

    def test_accepts_already_cased_collection(self):
        self.assertEqual(
            fix_collection_in_name("Виниловый ламинат Linderwood QUARTZ Адана LQ-01",
                                   "Quartz"),
            "Виниловый ламинат Linderwood Quartz Адана LQ-01")

    def test_idempotent(self):
        name = "Виниловый ламинат Linderwood Quartz Адана LQ-01"
        self.assertEqual(fix_collection_in_name(name, "QUARTZ"), name)

    def test_collection_absent_leaves_name(self):
        name = "Ламинат Peli Anatolia Дуб Сильвер"
        self.assertEqual(fix_collection_in_name(name, "QUARTZ"), name)

    def test_only_first_occurrence(self):
        """Второе совпадение — часть названия расцветки, его не трогаем."""
        self.assertEqual(
            fix_collection_in_name("Плитка ROMA Дуб ROMA", "ROMA"),
            "Плитка Roma Дуб ROMA")

    def test_empty_collection(self):
        name = "Ламинат Peli Дуб"
        self.assertEqual(fix_collection_in_name(name, ""), name)


class DropOwnArticleTest(unittest.TestCase):
    """§19.5: артикула в наименовании нет — он лежит в отдельном реквизите.

    Источник ошибки был не в модели, а в описании параметра сборки имени: «артикул ИЛИ
    размер». Агент честно ставил артикул, и на боевой базе так вышло 53 позиции.
    """

    def test_trailing_article_removed(self):
        self.assertEqual(
            drop_own_article("Ламинат Peli Vintage Ван Браун VN-511", "VN-511"),
            "Ламинат Peli Vintage Ван Браун")

    def test_multiword_article(self):
        self.assertEqual(
            drop_own_article("Ламинат Peli Platinium Сеньи AN PLT 911", "AN PLT 911"),
            "Ламинат Peli Platinium Сеньи")

    def test_article_in_the_middle(self):
        self.assertEqual(
            drop_own_article("Ламинат Peli LE-263 Натуральный венгерский", "LE-263"),
            "Ламинат Peli Натуральный венгерский")

    def test_case_insensitive(self):
        self.assertEqual(drop_own_article("Плитка Roma lq-01", "LQ-01"), "Плитка Roma")

    def test_similar_word_survives(self):
        """Сравнение по токенам: похожая подстрока внутри слова не режется."""
        self.assertEqual(drop_own_article("Ламинат Peli LE-2630 Дуб", "LE-263"),
                         "Ламинат Peli LE-2630 Дуб")

    def test_name_without_article_untouched(self):
        name = "Ламинат Peli Vintage Ван Браун"
        self.assertEqual(drop_own_article(name, "VN-511"), name)

    def test_empty_article(self):
        name = "Ламинат Peli Vintage Ван Браун"
        self.assertEqual(drop_own_article(name, ""), name)


if __name__ == "__main__":
    unittest.main()
