"""Категория для вопроса «где не хватает фото»: название → код вида товара 1С."""
import unittest

from src.price_tool import product_types
from src.website_tool.photo_scan import match_marks
from src.onec.client import TradeMark


class ResolveTest(unittest.TestCase):

    def test_exact_name_wins_over_substring(self):
        code, title = product_types.resolve("ламинат")
        self.assertEqual(title, "Ламинат")
        self.assertEqual(code, "000000003")

    def test_case_and_yo_do_not_matter(self):
        self.assertEqual(product_types.resolve("  ЛАМИНАТ ")[1], "Ламинат")

    def test_empty_is_refused_with_the_list(self):
        with self.assertRaises(ValueError) as err:
            product_types.resolve("")
        self.assertIn("Назови категорию", str(err.exception))
        self.assertIn("ламинат", str(err.exception))

    def test_unknown_is_named(self):
        with self.assertRaises(ValueError) as err:
            product_types.resolve("космолёты")
        self.assertIn("«космолёты» нет", str(err.exception))

    def test_every_known_type_resolves_to_itself(self):
        for title, code in product_types.known():
            self.assertEqual(product_types.resolve(title), (code, title))


class MatchMarksTest(unittest.TestCase):
    marks = [TradeMark(name="Classen / Классен", code="1"),
             TradeMark(name="Peli", code="2"),
             TradeMark(name="Peli Premium", code="3")]

    def test_either_half_of_a_bilingual_name(self):
        picked, unknown = match_marks(["Классен"], self.marks)
        self.assertEqual([m.code for m in picked], ["1"])
        self.assertEqual(unknown, [])

    def test_exact_half_beats_substring(self):
        picked, _ = match_marks(["peli"], self.marks)
        self.assertEqual([m.code for m in picked], ["2"])

    def test_unknown_is_returned_not_dropped(self):
        picked, unknown = match_marks(["нетакой"], self.marks)
        self.assertEqual(picked, [])
        self.assertEqual(unknown, ["нетакой"])


if __name__ == "__main__":
    unittest.main()
