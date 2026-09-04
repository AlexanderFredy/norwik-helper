"""Маппинг папок снятых с производства (§19.2.5)."""
import unittest

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

    def test_type_awaiting_folder_names_it(self):
        text = dis.refusal("000000028")
        self.assertIn("Обои", text)
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

    def test_kind_absent_is_not_a_problem(self):
        """Пока `folders` не выложен, kind может не приходить — это не повод отказывать."""
        self.assertEqual(dis.problems(_nodes(**{"YO-00006107": {"kind": ""}})), [])


if __name__ == "__main__":
    unittest.main()
