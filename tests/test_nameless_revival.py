"""Снятая карточка без артикула возвращается по точному описанию (решение админа 11.10.2026).

Позицию заводили без артикула, потом унесли в снятые, а поставщик её снова привёз. По
артикулу её не найти, и агент заводил дубль. Вернуть можно, когда описание совпало
В ТОЧНОСТИ — коллекция, размер, название расцветки — и кандидат один.
"""
import json
import unittest
from io import BytesIO

from src.model.task_builder import TaskBuilderTools, _revival_brief
from src.onec.client import NomItem
from tests.test_task_builder import FakeOnec


def workbook(*rows) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "Price"
    sheet.append(["Код", "Заводской код", "Наименование", "Опт"])
    for row in rows:
        sheet.append(list(row))
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def card(ref, site, article="", dead=True, length=1500, width=750, collection="Boost Pro"):
    return NomItem(ref=ref, id="", name=f"Керамогранит Atlas Concorde {collection} {site}",
                   article=article, unit="м2", size="", product_type="Керамогранит",
                   collection=collection, parent=collection, collection_ref="F-OLD",
                   alt_units={}, purchase=None, retail=None, rrc=None, site_name=site,
                   product_type_ref="000000011", length_from=length, length_to=length,
                   width_from=width, width_to=width, thickness=9, not_exported=dead,
                   tm="Atlas Concorde")


class Fake(FakeOnec):
    def __init__(self, items):
        self.items = items

    def by_tm_all(self, tm_code, **kw):
        class Nom:
            pass
        nom = Nom()
        nom.items, nom.tm, nom.total, nom.errors = list(self.items), "Atlas Concorde", 0, []
        return nom


ROW = ("+1", "A7G1", "Atlas Concorde Boost Pro Tobacco 75x150 (2pz)", "7330,20")


class NamelessRevivalTest(unittest.IsolatedAsyncioTestCase):

    async def compare(self, items, *rows):
        tools = TaskBuilderTools(workbook(*rows or (ROW,)), "Price.xlsx", onec=Fake(items))
        await tools.execute("read_price", {})
        out = json.loads(await tools.execute("compare_with_1c", {
            "tm_code": "T1", "collection": "Boost Pro", "articles": ["A7G1"]}))
        return tools, out

    async def test_exact_description_is_returned_with_the_article(self):
        tools, out = await self.compare([
            card("LIVE", "Tobacco", article="A28J", dead=False, length=750),
            card("YO-00058495", "Boost Pro Tobacco 75x150")])
        found = out.get("уже_есть_в_1С_вне_коллекции") or []
        self.assertTrue(any("YO-00058495" in line and "проставить" in line for line in found),
                        out)
        brief = _revival_brief(tools._revivals["boost pro"])
        self.assertIn("артикул в 1С ПУСТ — проставить A7G1", brief)
        self.assertIn("YO-00058495", brief)

    async def test_other_size_is_not_a_match(self):
        """75x75 — это другая плитка, хоть и тот же Tobacco."""
        _, out = await self.compare([
            card("LIVE", "Tobacco", article="A28J", dead=False, length=750),
            card("YO-1", "Boost Pro Tobacco 75x75", length=750, width=750)])
        self.assertNotIn("уже_есть_в_1С_вне_коллекции", out)

    async def test_two_candidates_mean_none(self):
        _, out = await self.compare([
            card("LIVE", "Tobacco", article="A28J", dead=False, length=750),
            card("YO-1", "Boost Pro Tobacco 75x150"), card("YO-2", "Tobacco 75x150")])
        self.assertNotIn("уже_есть_в_1С_вне_коллекции", out)

    async def test_card_with_an_article_is_not_reused(self):
        """У снятой карточки свой артикул — это другой товар, не наш."""
        _, out = await self.compare([
            card("LIVE", "Tobacco", article="A28J", dead=False, length=750),
            card("YO-1", "Boost Pro Tobacco 75x150", article="A21Q")])
        self.assertNotIn("уже_есть_в_1С_вне_коллекции", out)

    async def test_other_collection_is_not_a_match(self):
        _, out = await self.compare([
            card("LIVE", "Tobacco", article="A28J", dead=False, length=750),
            card("YO-1", "Arbor Tobacco 75x150", collection="Arbor")])
        self.assertNotIn("уже_есть_в_1С_вне_коллекции", out)


if __name__ == "__main__":
    unittest.main()
