"""Соответствие «код Артисана → заводской код» для обработки 1С (решение админа 06.10.2026).

Проверяется то, что ломается тихо: в колонке «Заводской код» прайса Артисаны стоит и то, что
артикулом не является («нет кода», одно значение дважды через перенос, код «0»). Записав такое
в «Артикул», мы сломали бы сверку на этих позициях — без единой ошибки.
"""
import csv
import re
import tempfile
import unittest
from pathlib import Path

from tools.artisana_codes import (Pair, clean_factory, pairs_from_sheet, shared_counts,
                                  write_csv)

HEAD = [
    ["Оптовый отдел", "", "", "", "", "", "", "", 'ООО "Артисан-Проект"'],
    ["Прайс-лист на 26.08.2026"],
    ["Код", "Заводской\nкод", "Вид", "Размер", "В упаковке", "", "Вес", "Ед.изм.",
     "Наименование", "Розн", "Опт"],
    ["", "Вид", "Вид", "Размер", "м2", "шт", "упак.", "Ед.изм.", "Наименование", "Розн",
     "Опт"],
]


class CleanTest(unittest.TestCase):

    def test_a_plain_code_passes(self):
        self.assertEqual(clean_factory("A019842"), ("A019842", ""))

    def test_empty_cell_gives_nothing_with_a_reason(self):
        value, reason = clean_factory("")
        self.assertEqual(value, "")
        self.assertIn("нет заводского кода", reason)

    def test_placeholder_is_not_a_code(self):
        """В прайсе у шести позиций буквально «нет кода» — записать его значило бы дать шести
        разным товарам один «артикул», да ещё и словами."""
        value, reason = clean_factory("нет кода")
        self.assertEqual(value, "")
        self.assertIn("нет кода", reason)
        self.assertEqual(clean_factory("НЕТ КОДА")[0], "")

    def test_one_value_twice_through_a_line_break_is_one_value(self):
        """Боевая ячейка: «44V179P\\r\\n44V179P»."""
        self.assertEqual(clean_factory("44V179P\r\n44V179P"), ("44V179P", ""))

    def test_two_different_values_in_one_cell_mean_none(self):
        """Выбирать между двумя кодами код не вправе: неверный артикул хуже отсутствующего."""
        value, reason = clean_factory("A01\nB02")
        self.assertEqual(value, "")
        self.assertIn("несколько разных кодов", reason)

    def test_a_scrap_is_not_a_code(self):
        """В прайсе Артисаны есть заводской код «0»."""
        self.assertEqual(clean_factory("0")[0], "")
        self.assertEqual(clean_factory("12")[0], "")

    def test_spaces_around_are_trimmed(self):
        self.assertEqual(clean_factory("  G100500009  "), ("G100500009", ""))


class SheetTest(unittest.TestCase):

    def rows(self, *data):
        return [list(r) for r in HEAD] + [list(r) for r in data]

    def test_columns_are_found_by_name_with_a_line_break(self):
        """Заголовок «Заводской код» в файле записан в две строки ячейки."""
        pairs = pairs_from_sheet(self.rows(["+23409", "A019842", "Панно"]))
        self.assertEqual(pairs, [Pair("+23409", "A019842")])

    def test_only_rows_with_a_plus_code_are_taken(self):
        """Строки брендов и коллекций («APE», «Armonia») кодом не являются."""
        pairs = pairs_from_sheet(self.rows(["APE"], ["", "Armonia"],
                                           ["+23409", "A019842", "Панно"]))
        self.assertEqual([p.code for p in pairs], ["+23409"])

    def test_a_row_without_a_factory_code_is_kept_with_a_reason(self):
        """Позиция без заводского кода попадает в выгрузку: обработка покажет её админу как
        «писать нечего», а не потеряет из списка."""
        pairs = pairs_from_sheet(self.rows(["+15433", "", "Плитка"]))
        self.assertEqual(pairs[0].factory, "")
        self.assertTrue(pairs[0].reason)

    def test_no_header_is_an_error(self):
        with self.assertRaises(ValueError):
            pairs_from_sheet([["что-то"], ["+1", "A1"]])


class SharedTest(unittest.TestCase):

    def test_a_factory_code_shared_by_two_artisana_codes_is_counted(self):
        """У 34 заводских кодов прайса по нескольку кодов Артисана: писать можно, но админ
        обязан видеть, где это случится."""
        pairs = [Pair("+28422", "G100500009"), Pair("+34069", "G100500009"),
                 Pair("+1", "A1B")]
        counts = shared_counts(pairs)
        self.assertEqual(counts["G100500009"], 2)
        self.assertEqual(counts["A1B"], 1)

    def test_empty_codes_are_not_shared_with_each_other(self):
        """Пустой заводской код у двухсот позиций — это не «общий код», а его отсутствие."""
        counts = shared_counts([Pair("+1", "", "нет"), Pair("+2", "", "нет")])
        self.assertEqual(counts, {})


class CsvTest(unittest.TestCase):

    def test_the_file_is_what_the_1c_processing_reads(self):
        """Формат — договорённость с обработкой 1С: «;», UTF-8, четыре колонки."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "out.csv"
            write_csv([Pair("+28422", "G100500009"), Pair("+34069", "G100500009"),
                       Pair("+15433", "", "в прайсе нет заводского кода")], path)
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "нужна метка UTF-8")
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle, delimiter=";"))

        self.assertEqual(rows[0], ["Код", "Заводской", "Повторов", "Причина"])
        self.assertEqual(rows[1], ["+28422", "G100500009", "2", ""])
        self.assertEqual(rows[3], ["+15433", "", "0", "в прайсе нет заводского кода"])


class ProcessingModuleTest(unittest.TestCase):
    """Модуль формы обработки 1С — статические проверки того, что ломается молча."""

    FORM = Path(__file__).resolve().parents[1] / "specs" / "1c" / \
        "artisana-articles-form-module.bsl"

    def setUp(self):
        self.text = self.FORM.read_text(encoding="utf-8")
        self.code = "\n".join(line for line in self.text.splitlines()
                              if not line.lstrip().startswith("//"))

    def test_the_main_form_attribute_is_not_overwritten(self):
        """В форме обработки `Объект` — её основной реквизит. Присвоив ему карточку товара,
        мы подменили бы саму обработку."""
        self.assertIsNone(re.search(r"^\s*Объект\s*=", self.code, re.M))

    def test_the_attribute_is_checked_before_writing(self):
        """Нет реквизита «Арт» — запись упала бы на первой же позиции."""
        check = self.code.find('Реквизиты.Найти("Арт")')
        write = self.code.find("Карточка.Записать()")
        self.assertGreater(check, 0)
        self.assertLess(check, write)

    def test_a_wrong_file_stops_before_writing(self):
        """Правило «нет в прайсе — пусто» на не том файле стёрло бы все артикулы разом."""
        stop = self.code.find("Соответствие.Количество() = 0")
        write = self.code.find("Карточка.Записать()")
        self.assertGreater(stop, 0)
        self.assertLess(stop, write)

    def test_a_code_missing_from_the_price_empties_the_article(self):
        """Решение админа 06.10.2026: «+» уходит в «Арт» всегда, «Артикул» без кода — пуст."""
        self.assertIn("Карточка.Арт = Код;", self.code)
        self.assertIn("Карточка.Артикул = Заводской;", self.code)
        self.assertRegex(self.code, r'Если Заводской = Неопределено Тогда\s+Заводской = "";')

    def test_no_manual_selection(self):
        """Одна кнопка, без таблицы с флажками."""
        self.assertNotIn("Пометка", self.code)


if __name__ == "__main__":
    unittest.main()
