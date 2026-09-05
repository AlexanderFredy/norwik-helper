"""Маппинг папок снятых с производства (§19.2.5)."""
import unittest
from unittest import mock

from src.price_tool import discontinued as dis


def _nodes(**overrides):
    """Дерево folders, где все папки маппинга на месте; overrides меняет отдельные узлы."""
    nodes = [
        {"ref": t.folder_ref, "name": t.folder_name, "kind": "discontinued"}
        for t in dis.MAPPING.values()
    ]
    for node in nodes:
        node.update(overrides.get(node["ref"], {}))
    return nodes


class MappingTest(unittest.TestCase):
    def test_known_type_gives_folder(self):
        self.assertEqual(dis.folder_for("000000003"), "YO-00006107")   # Ламинат

    def test_unknown_type_is_refusal_not_global_folder(self):
        """Молчаливый откат в глобальную папку — то, из чего каша и выросла."""
        self.assertIsNone(dis.folder_for("000000999"))
        self.assertNotIn("YO-00004421", str(dis.MAPPING))

    def test_empty_product_type_asks_admin(self):
        # среди невыгружаемых позиций вид товара реально бывает пуст (§19.3)
        self.assertIsNone(dis.folder_for(""))
        self.assertIsNone(dis.folder_for(None))
        self.assertIn("не заполнен вид товара", dis.refusal(""))

    def test_every_type_with_live_goods_is_mapped(self):
        """Обои и керамогранит закрыты папками 05.09.2026 — ждать больше нечего."""
        self.assertEqual(dis.NEEDS_FOLDER, {})
        self.assertEqual(dis.folder_for("000000028"), "YO-00078918")   # Обои
        self.assertEqual(dis.folder_for("000000011"), "YO-00012382")   # Керамогранит

    def test_type_awaiting_folder_names_it(self):
        """Ветка на будущее: в 1С завели вид товара, папку под него ещё нет."""
        with mock.patch.dict(dis.NEEDS_FOLDER, {"000000099": "Ковролин"}, clear=False):
            text = dis.refusal("000000099")
        self.assertIn("Ковролин", text)
        self.assertIn("нет отдельной папки", text)

    def test_refusal_mentions_code_of_unknown_type(self):
        self.assertIn("000000999", dis.refusal("000000999", "Половая тряпка"))

    def test_keys_are_1c_codes_and_folders_are_refs(self):
        for ref, target in dis.MAPPING.items():
            self.assertRegex(ref, r"^\d{9}$|^\d{6,9}$")
            self.assertTrue(target.folder_ref.startswith("YO-"), target.folder_ref)

    def test_one_folder_per_type_and_no_duplicates(self):
        folders = [t.folder_ref for t in dis.MAPPING.values()]
        self.assertEqual(len(folders), len(set(folders)))

    def test_types_awaiting_folder_are_not_mapped(self):
        for ref in dis.NEEDS_FOLDER:
            self.assertNotIn(ref, dis.MAPPING)


class ValidationTest(unittest.TestCase):
    def test_intact_tree_has_no_problems(self):
        self.assertEqual(dis.problems(_nodes()), [])

    def test_missing_folder_is_reported(self):
        nodes = [n for n in _nodes() if n["ref"] != "YO-00006107"]
        found = dis.problems(nodes)
        self.assertEqual(len(found), 1)
        self.assertIn("YO-00006107", found[0])
        self.assertIn("нет в 1С", found[0])

    def test_renamed_folder_is_reported(self):
        found = dis.problems(_nodes(**{"YO-00006107": {"name": "Архив ламината"}}))
        self.assertEqual(len(found), 1)
        self.assertIn("переименована", found[0])
        self.assertIn("Архив ламината", found[0])

    def test_kind_mismatch_is_reported(self):
        found = dis.problems(_nodes(**{"YO-00006107": {"kind": "collection"}}))
        self.assertEqual(len(found), 1)
        self.assertIn("не папкой снятых", found[0])

    def test_folder_without_known_name_is_only_checked_for_existence(self):
        """Папка заведена под нас, товаров в ней нет — имени мы не знаем и не сверяем."""
        found = dis.problems(_nodes(**{"YO-00078918": {"name": "что угодно"}}))
        self.assertEqual(found, [])

    def test_folder_without_known_name_still_must_exist(self):
        nodes = [n for n in _nodes() if n["ref"] != "YO-00078918"]
        found = dis.problems(nodes)
        self.assertEqual(len(found), 1)
        self.assertIn("Обои", found[0])

    def test_kind_absent_is_not_a_problem(self):
        """Пока `folders` не выложен, kind может не приходить — это не повод отказывать."""
        self.assertEqual(dis.problems(_nodes(**{"YO-00006107": {"kind": ""}})), [])


if __name__ == "__main__":
    unittest.main()
