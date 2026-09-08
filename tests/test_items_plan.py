"""План правок справочника (§19.5, §19.8, §19.9).

Случаи здесь не выдуманы: почти каждый — то, на чём разбор LINDERWOOD уже спотыкался
вживую. Артикул, дописанный в наименование для сайта; коллекция капсом; правка, которой
нечего править; позиция, которой нет в выгрузке 1С.
"""
import unittest

from src.onec.client import ItemProperty, NomItem
from src.price_tool.items import (build_name, plan_collection, render, significant,
                                  site_name)


def _nom(**kw) -> NomItem:
    base = dict(ref="YO-1", id="1", name="Виниловый ламинат Linderwood Quartz Адана LQ-01",
                article="LQ-01", unit="м2", size="1219x228x4",
                product_type="Виниловый ламинат", collection="Quartz",
                parent="Quartz", collection_ref="YO-00078954",
                alt_units={"упак": 2.23}, purchase=None, retail=None, rrc=None,
                full_name="Виниловый ламинат Linderwood Quartz Адана LQ-01",
                site_name="Адана", product_type_ref="000000002",
                collection_code="0004046", length_from=1219.0, length_to=1219.0,
                width_from=228.0, width_to=228.0, thickness=4.0,
                properties=(ItemProperty("Класс", "0000002", "43 класс", "0000017"),))
    base.update(kw)
    return NomItem(**base)


def _inp(**kw) -> dict:
    base = dict(tm_code="000000325", tm_name="Linderwood", product_type="000000002",
                product_type_name="Виниловый ламинат", collection="QUARTZ", items=[])
    base.update(kw)
    return base


class BuildNameTest(unittest.TestCase):

    def test_parts_assembled_in_order(self):
        self.assertEqual(
            build_name("Виниловый ламинат", "Linderwood", "QUARTZ", "Адана", "LQ-01"),
            "Виниловый ламинат Linderwood Quartz Адана LQ-01")

    def test_legacy_type_replaced_not_appended(self):
        """«Водостойкий ламинат» — архаизм; дописать вид товара спереди было бы порчей."""
        self.assertEqual(
            build_name("Виниловый ламинат", "", "", "Водостойкий ламинат Vinilam Дуб", ""),
            "Виниловый ламинат Vinilam Дуб")

    def test_empty_tail_is_normal(self):
        self.assertEqual(build_name("Ламинат", "Peli", "Anatolia", "Дуб Сильвер"),
                         "Ламинат Peli Anatolia Дуб Сильвер")

    def test_site_name_is_title_only(self):
        """Артикул в наименовании для сайта — отсебятина, её на бою уже убирали."""
        self.assertEqual(site_name("Адана"), "Адана")


class SignificanceTest(unittest.TestCase):

    def test_case_and_spaces_are_not_a_change(self):
        self.assertFalse(significant("Дуб  МЕДОВЫЙ", "Дуб Медовый"))

    def test_real_change(self):
        self.assertTrue(significant("Дуб Медовый", "Дуб Тёмный"))

    def test_numbers_compared_as_is(self):
        self.assertTrue(significant(1.84, 2.23))
        self.assertFalse(significant(2.23, 2.23))


class PlanTest(unittest.TestCase):

    def test_creation_builds_full_operation(self):
        plan = plan_collection(_inp(items=[
            {"op": "create", "article": "LQ-09", "title": "Ялова", "tail": "LQ-09",
             "unit": "м2", "pack_coefficient": 2.23, "length": 1219, "width": 228,
             "thickness": 4,
             "properties": [{"property": "0000002", "value_code": "0000017"}]}
        ]), current=[])
        op = plan.ops()[0]
        self.assertEqual(op["op"], "create_item")
        self.assertEqual(op["name"], "Виниловый ламинат Linderwood Quartz Ялова LQ-09")
        self.assertEqual(op["site_name"], "Ялова")
        self.assertEqual(op["manufacturer"], "000000325")
        self.assertEqual((op["length_from"], op["length_to"]), (1219.0, 1219.0))
        self.assertEqual(op["properties"], [{"property": "0000002",
                                             "value_code": "0000017"}])

    def test_new_folder_first_and_items_reference_it(self):
        """Кода у папки ещё нет — товары ссылаются на неё через $id (§19.2.3)."""
        plan = plan_collection(_inp(
            new_folder={"parent_ref": "YO-00078953", "name": "QUARTZ"},
            items=[{"op": "create", "article": "LQ-09", "title": "Ялова"}]), current=[])
        ops = plan.ops()
        self.assertEqual(ops[0]["op"], "create_folder")
        self.assertEqual(ops[0]["name"], "Quartz")
        self.assertEqual(ops[1]["parent_ref"], "$collection")

    def test_update_sends_only_changed_fields(self):
        """Отсутствие поля значит «не трогать»: полную карточку модель не присылает."""
        plan = plan_collection(_inp(items=[
            {"op": "update", "ref": "YO-1", "article": "LQ-01", "title": "Адана",
             "tail": "LQ-01", "pack_coefficient": 2.5}
        ]), current=[_nom()])
        op = plan.ops()[0]
        self.assertEqual(op["op"], "update_item")
        self.assertEqual(op["pack_coefficient"], 2.5)
        self.assertNotIn("thickness", op)
        self.assertNotIn("unit", op)

    def test_nothing_to_change_is_not_an_operation(self):
        plan = plan_collection(_inp(items=[
            {"op": "update", "ref": "YO-1", "article": "LQ-01", "title": "Адана",
             "tail": "LQ-01", "thickness": 4}
        ]), current=[_nom()])
        self.assertEqual(plan.ops(), [])
        self.assertIn("Расхождений нет", render(plan))

    def test_case_only_rename_is_normalization(self):
        """Правка есть, но вся она — регистр: админу одной строкой, в историю ничего."""
        old = _nom(name="Виниловый ламинат Linderwood QUARTZ Адана LQ-01",
                   full_name="Виниловый ламинат Linderwood QUARTZ Адана LQ-01")
        plan = plan_collection(_inp(items=[
            {"op": "update", "ref": "YO-1", "article": "LQ-01", "title": "Адана",
             "tail": "LQ-01"}
        ]), current=[old])
        self.assertEqual(len(plan.normalized), 1)
        self.assertEqual(plan.updated, [])
        self.assertIn("Нормализация", render(plan))

    def test_missing_item_is_a_warning_not_a_creation(self):
        """Кода нет в выгрузке — молча создавать вместо правки нельзя."""
        plan = plan_collection(_inp(items=[
            {"op": "update", "ref": "YO-9999", "article": "LQ-77", "title": "Ялова"}
        ]), current=[_nom()])
        self.assertIn("нет в выгрузке 1С", render(plan))
        self.assertEqual(plan.ops(), [])

    def test_property_already_set_is_not_resent(self):
        plan = plan_collection(_inp(items=[
            {"op": "update", "ref": "YO-1", "article": "LQ-01", "title": "Адана",
             "tail": "LQ-01",
             "properties": [{"property": "0000002", "value_code": "0000017"}]}
        ]), current=[_nom()])
        self.assertEqual(plan.ops(), [])

    def test_new_property_value_goes_out(self):
        plan = plan_collection(_inp(items=[
            {"op": "update", "ref": "YO-1", "article": "LQ-01", "title": "Адана",
             "tail": "LQ-01",
             "properties": [{"property": "0000008", "value_code": "0000016"}]}
        ]), current=[_nom()])
        self.assertEqual(plan.ops()[0]["properties"],
                         [{"property": "0000008", "value_code": "0000016"}])

    def test_out_of_scope_type_refuses(self):
        """Категории задаёт админ командой /categories — обойти их рассуждением нельзя."""
        plan = plan_collection(_inp(items=[{"op": "create", "article": "X", "title": "Y"}]),
                               current=[], scope=["плитка"])
        self.assertEqual(plan.ops(), [])
        self.assertIn("вне категорий", render(plan))

    def test_scope_match_is_loose(self):
        """«ламинат» покрывает «Виниловый ламинат», как в scope.py."""
        plan = plan_collection(_inp(items=[{"op": "create", "article": "X", "title": "Y"}]),
                               current=[], scope=["ламинат"])
        self.assertEqual(len(plan.ops()), 1)

    def test_parent_move_is_a_change(self):
        plan = plan_collection(_inp(items=[
            {"op": "update", "ref": "YO-1", "article": "LQ-01", "title": "Адана",
             "tail": "LQ-01", "parent_ref": "YO-00004421"}
        ]), current=[_nom()])
        self.assertEqual(plan.ops()[0]["parent_ref"], "YO-00004421")
        self.assertIn("папка", render(plan))


class RenderTest(unittest.TestCase):

    def test_same_change_across_collection_is_one_line(self):
        """Сорок раз «коэффициент 1.8 → 2.23» — это стена, а не отчёт (§19.9)."""
        current = [_nom(ref=f"YO-{n}", alt_units={"упак": 1.84}) for n in range(4)]
        plan = plan_collection(_inp(items=[
            {"op": "update", "ref": f"YO-{n}", "article": "LQ-01", "title": "Адана",
             "tail": "LQ-01", "pack_coefficient": 2.23} for n in range(4)
        ]), current=current)
        text = render(plan)
        self.assertIn("вся коллекция", text)
        self.assertEqual(text.count("коэффициент упаковки"), 1)

    def test_few_items_named_individually(self):
        current = [_nom(ref="YO-1", alt_units={"упак": 1.84}),
                   _nom(ref="YO-2", article="LQ-02", alt_units={"упак": 2.23})]
        plan = plan_collection(_inp(items=[
            {"op": "update", "ref": "YO-1", "article": "LQ-01", "title": "Адана",
             "tail": "LQ-01", "pack_coefficient": 2.23}
        ]), current=current)
        self.assertIn("LQ-01", render(plan))

    def test_creation_list_is_capped(self):
        plan = plan_collection(_inp(items=[
            {"op": "create", "article": f"LQ-{n:02}", "title": f"Цвет {n}"}
            for n in range(15)
        ]), current=[])
        text = render(plan)
        self.assertIn("Новых позиций: 15", text)
        self.assertIn("и ещё 5", text)


if __name__ == "__main__":
    unittest.main()
