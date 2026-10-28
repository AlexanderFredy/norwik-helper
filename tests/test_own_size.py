"""Свой размер в названии расцветки снимается (просьба админа 11.10.2026, YO-00073555).

У плитки размер пишется в конце имени отдельно (§19.5), а поставщик дублирует его в
названии: «Calacatta Meraviglia 120x278 - 6mm Lapp.» при полях 2780 × 1200 давало
«…120x278 - 6mm Lapp. 2780x1200».
"""
import unittest

from src.onec.client import NomItem
from src.price_tool.items import plan_collection
from src.price_tool.naming import drop_own_size


class DropOwnSizeTest(unittest.TestCase):

    def test_size_in_centimetres_and_reversed(self):
        self.assertEqual(drop_own_size("Calacatta Meraviglia 120x278 - 6mm Lapp.", 2780, 1200,
                                       6), ("Calacatta Meraviglia Lapp.", None))

    def test_empty_thickness_field_gets_the_value(self):
        """Толщина стояла только в названии — её надо положить в поле, а не потерять."""
        self.assertEqual(drop_own_size("Black Origin 120x278 - 6mm Lapp.", 2780, 1200, 0),
                         ("Black Origin Lapp.", 6.0))

    def test_millimetres_and_units(self):
        self.assertEqual(drop_own_size("Grey 60x120 см", 1200, 600, 9)[0], "Grey")
        self.assertEqual(drop_own_size("Grey 1200x600x9 мм", 1200, 600, 9)[0], "Grey")

    def test_leftover_commas_and_bracket_are_cleaned(self):
        """Porcelanosa Oxford (27.10.2026): «NATURAL, 12.5мм, 33.3x100 (» оставляло
        «NATURAL, , (» после снятия размера и толщины."""
        self.assertEqual(drop_own_size("LISTON NATURAL, 12.5мм, 33.3x100 (", 1000, 330,
                                       12.5)[0], "LISTON NATURAL")

    def test_meaningful_commas_and_brackets_stay(self):
        self.assertEqual(drop_own_size("Deco Topo (A)", 1500, 596, 11)[0], "Deco Topo (A)")
        self.assertEqual(drop_own_size("Gris, Lappato", 1200, 600, 9)[0], "Gris, Lappato")

    def test_foreign_size_stays(self):
        """Чип мозаики на листе 300 × 300 — это описание товара, а не его размер."""
        self.assertEqual(drop_own_size("Mosaic 2,5x2,5", 300, 300, 8)[0], "Mosaic 2,5x2,5")

    def test_other_thickness_stays(self):
        self.assertEqual(drop_own_size("Дуб 9,5mm", 1290, 190, 8)[0], "Дуб 9,5mm")

    def test_numbers_that_are_not_sizes_stay(self):
        self.assertEqual(drop_own_size("Bleu 40", 800, 400, 8)[0], "Bleu 40")
        self.assertEqual(drop_own_size("Дуб 33 класс", 1290, 190, 8)[0], "Дуб 33 класс")


class PlanTest(unittest.TestCase):

    def card(self):
        return NomItem(
            ref="YO-00073555", id="", name="old", article="AJH2", unit="м2",
            size="2780x1200x0", product_type="Керамическая плитка", collection="Marvel Meraviglia",
            parent="Marvel Meraviglia", collection_ref="F", alt_units={}, purchase=None,
            retail=None, rrc=None, site_name="Calacatta Meraviglia 120x278 - 6mm Lapp.",
            product_type_ref="000000010", length_from=2780, length_to=2780,
            width_from=1200, width_to=1200, thickness=0)

    def test_normalization_cleans_the_title_and_fills_the_thickness(self):
        plan = plan_collection({
            "tm_code": "000000115", "tm_name": "Atlas Concorde / Атлас конкорд",
            "product_type": "000000010", "product_type_name": "Керамическая плитка",
            "collection": "Marvel Meraviglia",
            "items": [{"op": "update", "ref": "YO-00073555", "article": "AJH2",
                       "title": "Calacatta Meraviglia 120x278 - 6mm Lapp.",
                       "tail": "2780x1200x0"}]}, [self.card()], [])
        item = plan.items[0]
        self.assertEqual(item.name, "Керамическая плитка Atlas Concorde Marvel Meraviglia "
                                    "Calacatta Meraviglia Lapp. 278x120x0,6")
        self.assertEqual(item.fields.get("thickness"), 6.0)
        self.assertTrue(any("убран размер" in w for w in item.warnings))


class TileSizeTest(unittest.TestCase):
    """Плитка: толщина не важна, длина и ширина ±1 см (правило админа 28.10.2026)."""

    def test_rounded_and_precise_sizes_are_the_same(self):
        from src.price_tool.naming import tile_size_in
        self.assertTrue(tile_size_in("Mark Ivory 60x120", 1197, 597))
        self.assertTrue(tile_size_in("Mark Ivory 59,7x119,7", 1200, 600))
        self.assertTrue(tile_size_in("Mark Ivory 600x1200 мм", 1197, 597))

    def test_thickness_does_not_matter(self):
        from src.price_tool.naming import tile_size_in
        self.assertTrue(tile_size_in("Boost 60x120x0,9 Lapp", 1200, 600))
        self.assertTrue(tile_size_in("Boost 60x120x2", 1200, 600))

    def test_tolerance_is_one_centimetre(self):
        from src.price_tool.naming import tile_size_in
        self.assertTrue(tile_size_in("61x120", 1200, 600))
        self.assertFalse(tile_size_in("62x120", 1200, 600))

    def test_another_format_is_not_the_same(self):
        """Mark Chrome: 75x150 в прайсе и 600x600 в 1С — разный товар."""
        from src.price_tool.naming import tile_size_in
        self.assertFalse(tile_size_in("Mark Chrome 75x150 R10", 600, 600))
        self.assertIsNone(tile_size_in("Mark Chrome R10", 600, 600))
        self.assertIsNone(tile_size_in("Mark Chrome 75x150", None, None))


class SizeInCentimetresTest(unittest.TestCase):
    """В полях — мм, в наименовании — см с одним разрядом, если он не ноль (28.10.2026)."""

    def test_millimetres_become_centimetres(self):
        from src.price_tool.naming import size_in_cm
        self.assertEqual(size_in_cm("333x1000", 1000, 333), "33,3x100")
        self.assertEqual(size_in_cm("600x1200x9", 1200, 600, 9), "60x120x0,9")

    def test_centimetres_are_kept_and_tidied(self):
        from src.price_tool.naming import size_in_cm
        self.assertEqual(size_in_cm("33,3x100", 1000, 333), "33,3x100")
        self.assertEqual(size_in_cm("60.0x120", 1200, 600), "60x120")

    def test_foreign_or_unknown_numbers_are_left_alone(self):
        """Чужой размер или карточка без полей — не угадываем единицы."""
        from src.price_tool.naming import size_in_cm
        self.assertEqual(size_in_cm("300x300", 1000, 333), "300x300")
        self.assertEqual(size_in_cm("333x1000", None, None), "333x1000")

    def test_dayton_sand_keeps_its_centimetres(self):
        """БОЙ 28.10.2026: «Dayton Sand 33,3x100» нормализация переименовала в
        «Porcelanosa Dayton Sand 333x1000» — хвост взят из поля `size` карточки (мм)."""
        card = NomItem(ref="D1", id="", name="Керамическая плитка Dayton Sand 33,3x100",
                       article="", unit="м2", size="333x1000",
                       product_type="Керамическая плитка", collection="Dayton",
                       parent="Dayton", collection_ref="F", alt_units={}, purchase=None,
                       retail=None, rrc=None, site_name="Sand", product_type_ref="000000010",
                       length_from=1000, length_to=1000, width_from=333, width_to=333,
                       thickness=0)
        plan = plan_collection({"tm_code": "000000132", "tm_name": "Porcelanosa",
                                "collection": "Dayton", "product_type": "000000010",
                                "product_type_name": "Керамическая плитка",
                                "items": [{"op": "update", "ref": "D1", "title": "Sand",
                                           "tail": "333x1000"}]}, [card], [])
        item = plan.items[0]
        self.assertEqual(item.name, "Керамическая плитка Porcelanosa Dayton Sand 33,3x100")
        self.assertEqual(item.full_name, item.name)


class FullNameSizeTest(unittest.TestCase):
    """У керамики размер — в конце и ПОЛНОГО наименования (решение админа 27.10.2026)."""

    BASE = {"tm_code": "000000132", "tm_name": "Porcelanosa", "collection": "Persia"}
    CERAMIC = {"product_type": "000000010", "product_type_name": "Керамическая плитка"}

    def card(self, **kw):
        fields = dict(ref="P1", id="", name="old", article="", unit="м2", size="333x1000x10",
                      product_type="Керамическая плитка", collection="Persia",
                      parent="Persia", collection_ref="F", alt_units={}, purchase=None,
                      retail=None, rrc=None, site_name="Bone", product_type_ref="000000010",
                      length_from=1000, length_to=1000, width_from=333, width_to=333,
                      thickness=10)
        fields.update(kw)
        return NomItem(**fields)

    def full(self, row, kind=None, card=None):
        plan = plan_collection({**self.BASE, **(kind or self.CERAMIC), "items": [row]},
                               [card or self.card()], [])
        return plan.items[0].full_name

    def test_size_is_appended_to_a_full_name_without_it(self):
        self.assertEqual(
            self.full({"op": "update", "ref": "P1", "title": "Bone", "tail": "333x1000x10",
                       "full_name": "Керамическая плитка Porcelanosa Persia Bone"}),
            "Керамическая плитка Porcelanosa Persia Bone 33,3x100x1")

    def test_size_written_otherwise_is_not_doubled(self):
        """«33,3x100» в сантиметрах — тот же размер: снимаем и ставим в конец один раз."""
        self.assertEqual(
            self.full({"op": "update", "ref": "P1", "title": "Bone", "tail": "333x1000x10",
                       "full_name": "Керамическая плитка Porcelanosa Persia Bone 33,3x100"}),
            "Керамическая плитка Porcelanosa Persia Bone 33,3x100x1")

    def test_already_at_the_end_stays_as_is(self):
        self.assertEqual(
            self.full({"op": "update", "ref": "P1", "title": "Bone", "tail": "333x1000x10"}),
            "Керамическая плитка Porcelanosa Persia Bone 33,3x100x1")

    def test_laminate_full_name_gets_no_size(self):
        """У ламината размер живёт в имени папки, а не в карточке (§19.5)."""
        laminate = {"product_type": "000000003", "product_type_name": "Ламинат"}
        card = self.card(product_type="Ламинат", product_type_ref="000000003",
                         size="1290x190x8")
        self.assertEqual(
            self.full({"op": "update", "ref": "P1", "title": "Bone", "tail": "1290x190x8",
                       "full_name": "Ламинат Porcelanosa Persia Bone"}, laminate, card),
            "Ламинат Porcelanosa Persia Bone")


if __name__ == "__main__":
    unittest.main()
