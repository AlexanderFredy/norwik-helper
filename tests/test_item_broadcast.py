"""Уведомления о правках справочника (§19.9)."""
import unittest

from src.price_tool.item_broadcast import build_item_broadcast


def digest(items, total=None, collection="Marvel Pro", tm="Atlas Concorde"):
    return {"supplier": "Артисан-Проект",
            "groups": [{"tm_name": tm, "collection": collection,
                        "total": total, "items": items}]}


class ManagerTest(unittest.TestCase):
    """Менеджеру про нормализацию не говорим вовсе."""

    def test_normalization_only_gives_nothing(self):
        d = digest([{"ref": "A", "normalized": True},
                    {"ref": "B", "normalized": True}])
        self.assertIsNone(build_item_broadcast(d))

    def test_normalization_hidden_but_real_change_shown(self):
        d = digest([{"ref": "A", "normalized": True},
                    {"ref": "B", "changes": {"size": ["1290x157x12", "1290x190x12"]}}])
        text = build_item_broadcast(d)
        self.assertIn("размер 1290x157x12 → 1290x190x12", text)
        self.assertNotIn("нормализация", text)

    def test_collection_without_changes_drops_tm_header(self):
        d = digest([{"ref": "A", "normalized": True}])
        self.assertIsNone(build_item_broadcast(d))


class AdminTest(unittest.TestCase):
    """Админу — фактом и без позиций."""

    def test_normalization_mentioned_briefly(self):
        d = digest([{"ref": "YO-00074782", "normalized": True},
                    {"ref": "YO-00074783", "normalized": True}], total=12)
        text = build_item_broadcast(d, for_admin=True)
        self.assertIn("нормализация (2 поз.)", text)
        # Позиции не перечисляются: админу нужен факт чистки, а не список пробелов
        self.assertNotIn("YO-00074782", text)

    def test_whole_collection_normalized(self):
        d = digest([{"ref": "A", "normalized": True},
                    {"ref": "B", "normalized": True}], total=2)
        self.assertIn("нормализация (вся коллекция)",
                      build_item_broadcast(d, for_admin=True))


class CollapseTest(unittest.TestCase):
    """Одинаковое по коллекции — одной строкой, а не списком позиций."""

    def test_same_change_stated_once(self):
        items = [{"ref": r, "changes": {"pack": [1.84, 2.0]}} for r in "ABCDE"]
        text = build_item_broadcast(digest(items, total=5))
        self.assertIn("коэффициент упаковки 1.84 → 2 (вся коллекция)", text)
        for ref in "ABCDE":
            self.assertNotIn(f" {ref} ", text)

    def test_partial_collection_counts_positions(self):
        items = [{"ref": r, "changes": {"pack": [1.84, 2.0]}} for r in "AB"]
        self.assertIn("(2 поз.)", build_item_broadcast(digest(items, total=10)))

    def test_different_new_values_collapse_to_count(self):
        items = [{"ref": "A", "changes": {"size": ["1", "2"]}},
                 {"ref": "B", "changes": {"size": ["1", "3"]}}]
        self.assertIn("размер у 2 поз.", build_item_broadcast(digest(items)))

    def test_same_new_different_old(self):
        items = [{"ref": "A", "changes": {"size": ["1", "9"]}},
                 {"ref": "B", "changes": {"size": ["2", "9"]}}]
        self.assertIn("размер → 9", build_item_broadcast(digest(items)))


class FieldsTest(unittest.TestCase):
    def test_all_reportable_fields(self):
        items = [{"ref": "A", "changes": {
            "parent": ["Керамическая плитка", "Керамогранит"],
            "collection": ["Klif", "Klif Grey"],
            "pack": [1.84, 2.0],
            "size": ["1290x157x12", "1290x190x12"],
            "prices": {"purchase": [821, 900]},
        }}]
        text = build_item_broadcast(digest(items))
        # «закуп» — тот же ярлык, что в ценовом broadcast и в /history: словарь один
        for expect in ("папка", "коллекция", "коэффициент упаковки", "размер", "закуп"):
            self.assertIn(expect, text)

    def test_empty_old_value_reads_as_net(self):
        items = [{"ref": "A", "changes": {"pack": [None, 1.84]}}]
        self.assertIn("нет → 1.84", build_item_broadcast(digest(items)))

    def test_tm_header_present(self):
        items = [{"ref": "A", "changes": {"size": ["1", "2"]}}]
        text = build_item_broadcast(digest(items))
        self.assertIn("— Atlas Concorde", text)
        self.assertIn("Артисан-Проект", text)

    def test_empty_digest(self):
        self.assertIsNone(build_item_broadcast({"groups": []}))
        self.assertIsNone(build_item_broadcast({}))


if __name__ == "__main__":
    unittest.main()
