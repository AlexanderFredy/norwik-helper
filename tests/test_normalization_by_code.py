"""Нормализацию заводит КОД, а не модель (бой 12.10.2026, прайс Эггера).

На одном и том же прайсе Haiku заводила её через раз, без единого отказа от кода, — а
нужна ли она, код знает сам: модель наименований 1С не видит вовсе.
"""
import unittest
from types import SimpleNamespace

from src.model.enums import TaskKind
from src.model.task_builder import _add_normalization_by_code


class FakeTools:
    def __init__(self, touched, coverage, refuse=()):
        self._touched = {code: set() for code in touched}
        self._coverage = coverage
        self._refuse_for = set(refuse)
        self._tm_names = {"000000006": "Egger / Эггер", "000000007": "AGT"}
        self.collected = []
        self.asked = []

    def _normalization_coverage(self, code):
        return self._coverage.get(code, (0, 0))

    def _add(self, inp):
        self.asked.append(inp)
        if inp["tm_code"] in self._refuse_for:
            return "менять нечего"
        self.collected.append(SimpleNamespace(
            kind=TaskKind.NORMALIZE_NAMES,
            address=SimpleNamespace(tm=SimpleNamespace(code=inp["tm_code"]))))
        return "заведена"


class NormalizationByCodeTest(unittest.TestCase):

    def test_added_for_the_price_mark(self):
        tools = FakeTools(["000000006"], {"000000006": (5, 6)})
        self.assertEqual(_add_normalization_by_code(tools), ["Egger / Эггер"])
        self.assertEqual(tools.asked[0]["kind"], TaskKind.NORMALIZE_NAMES.value)

    def test_not_for_a_foreign_mark(self):
        """Прайс закрыл меньше половины коллекций — это не его марка (бой 24.09.2026)."""
        tools = FakeTools(["000000007"], {"000000007": (2, 7)})
        self.assertEqual(_add_normalization_by_code(tools), [])
        self.assertEqual(tools.asked, [])

    def test_not_twice_when_the_model_already_did(self):
        tools = FakeTools(["000000006"], {"000000006": (5, 6)})
        tools._add({"kind": "x", "tm": "Egger", "tm_code": "000000006"})
        tools.asked.clear()
        self.assertEqual(_add_normalization_by_code(tools), [])
        self.assertEqual(tools.asked, [])

    def test_nothing_to_change_is_not_a_task(self):
        """«Менять нечего» решает та же проверка, что у вызова моделью."""
        tools = FakeTools(["000000006"], {"000000006": (5, 6)}, refuse=["000000006"])
        self.assertEqual(_add_normalization_by_code(tools), [])
        self.assertEqual(tools.collected, [])


if __name__ == "__main__":
    unittest.main()
