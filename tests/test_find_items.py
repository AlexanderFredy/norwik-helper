"""Поиск по всей номенклатуре перед созданием позиции (§19.11).

Проверка «нет ли товара среди снятых» шла по выгрузке ОДНОЙ марки — и ровно тот случай,
ради которого она задумана, не ловился: товар, заведённый когда-то под ДРУГОЙ маркой, в
выгрузке запрошенной ТМ не появляется. Агент создавал дубль, и в справочнике оказывались
две карточки одного товара: поставщик сменил бренд, марку переименовали, позицию завели не
в ту ветку.

Обойти это существующими эндпоинтами было нечем: by-tm требует марку, folders отдаёт только
папки, а перебор всех марок — это тысячи позиций в контекст на каждую строку прайса.
"""
import json
import tempfile
import unittest
from pathlib import Path

from src.agent.pricing_tools import PricingTools, PRICING_TOOLS, clear_nomenclature_cache
from src.onec.client import FoundItem
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec, item


def found(ref, name, article="", tm="Egger", tm_code="T1", not_exported=False,
          folder="Снятые с производства"):
    return FoundItem(ref=ref, name=name, article=article, tm=tm, tm_code=tm_code,
                     parent_ref="F-1", parent_name=folder, not_exported=not_exported)


class Base(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec([item("YO-1", 949, 1649, 1139)])
        self.tools = PricingTools(self.onec, self.store, 42)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def find(self, **inp) -> dict:
        return json.loads(await self.tools.execute("find_1c_items", inp))


class ToolTest(Base):

    async def test_tool_is_offered_to_the_model(self):
        self.assertIn("find_1c_items", {t["name"] for t in PRICING_TOOLS})

    async def test_finds_a_discontinued_item_under_another_mark(self):
        """Главный сценарий: артикул из прайса Egger, а карточка лежит под A+ FLOOR."""
        self.onec.catalogue = [found("YO-77", "Ламинат Дуб Верона", article="A001",
                                     tm="A+ FLOOR", tm_code="T9", not_exported=True)]
        out = await self.find(article="A001")
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["items"][0]["tm"], "A+ FLOOR")
        self.assertTrue(out["items"][0]["not_exported"])
        self.assertEqual(out["items"][0]["ref"], "YO-77")

    async def test_empty_result_is_the_permission_to_create(self):
        self.onec.catalogue = [found("YO-1", "Другое", article="ZZZ")]
        out = await self.find(article="A001")
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["items"], [])

    async def test_search_by_name_when_there_is_no_article(self):
        self.onec.catalogue = [found("YO-5", "Ламинат Дуб Медовый")]
        self.assertEqual((await self.find(name="Дуб Медовый"))["total"], 1)

    async def test_query_is_echoed_back(self):
        """Ответ должен говорить, ЧТО искали: иначе пустой результат нечем объяснить."""
        out = await self.find(article="A001")
        self.assertEqual(out["query"]["article"], "A001")

    async def test_refuses_an_empty_query(self):
        out = await self.find()
        self.assertIn("error", out)
        self.assertEqual(self.onec.searches, [])      # в 1С не ходили

    async def test_folder_is_reported(self):
        """Агенту нужно сказать админу, ГДЕ лежит найденное."""
        self.onec.catalogue = [found("YO-9", "Ламинат", article="A001",
                                     folder="Снятые ламинат", not_exported=True)]
        out = await self.find(article="A001")
        self.assertEqual(out["items"][0]["folder"], "Снятые ламинат")
        self.assertEqual(out["items"][0]["folder_ref"], "F-1")

    async def test_tm_narrows_the_search(self):
        self.onec.catalogue = [found("YO-1", "A", article="A001", tm_code="T1"),
                               found("YO-2", "B", article="A001", tm_code="T9")]
        self.assertEqual((await self.find(article="A001"))["total"], 2)
        self.assertEqual((await self.find(article="A001", tm="T9"))["total"], 1)

    async def test_batch_checks_a_whole_collection_in_one_call(self):
        """Главная мера экономии: один вызов на коллекцию вместо одного на позицию.

        Цена определяется ЧИСЛОМ вызовов, а не объёмом ответа: круг ручного цикла несёт всю
        историю и на боевом прогоне стоил $0.097, тогда как сам ответ — $0.0002. Поштучная
        проверка 53 позиций LINDERWOOD обошлась бы дороже всего прогона.
        """
        self.onec.catalogue = [found("YO-1", "Дуб Верона", article="A001"),
                               found("YO-2", "Дуб Медовый", article="A007")]
        out = await self.find(articles=["A001", "A002", "A003", "A007"])
        self.assertEqual(len(self.onec.searches), 1)          # один поход в 1С
        self.assertEqual(out["total"], 2)
        self.assertEqual({i["article"] for i in out["items"]}, {"A001", "A007"})

    async def test_batch_query_is_echoed_for_matching_up(self):
        """Ответ должен позволять сопоставить найденное с запрошенным."""
        out = await self.find(articles=["A001", "A002"])
        self.assertEqual(out["query"]["articles"], ["A001", "A002"])

    async def test_batch_is_capped_before_going_to_1c(self):
        out = await self.find(articles=[f"A{i:04}" for i in range(150)])
        self.assertIn("error", out)
        self.assertEqual(self.onec.searches, [])

    async def test_blank_entries_in_the_batch_are_ignored(self):
        self.onec.catalogue = [found("YO-1", "Дуб", article="A001")]
        out = await self.find(articles=["A001", "", "   "])
        self.assertEqual(out["total"], 1)

    async def test_truncated_is_distinct_from_empty(self):
        """«Не нашлось» и «не поместилось» — разные ответы."""
        self.onec.catalogue = [found(f"YO-{i}", "Ламинат", article="A001")
                               for i in range(10)]
        out = await self.find(article="A001", limit=3)
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["items"]), 3)


class ClientTest(unittest.TestCase):
    """Разбор ответа 1С: поля берутся из того же конверта, что у by-tm."""

    def test_parses_the_envelope(self):
        import httpx

        from src.onec.client import OnecClient

        payload = {"total": 1, "truncated": False, "errors": [],
                   "items": [{"ref": "YO-77", "name": "Ламинат Дуб Верона",
                              "full_name": "Ламинат A+ FLOOR Дуб Верона",
                              "article": "A001", "unit": "м2",
                              "product_type": "Ламинат", "product_type_ref": "YO-3",
                              "tm": "A+ FLOOR", "tm_code": "T9",
                              "parent": {"code": "F-9", "name": "Снятые"},
                              "not_exported": True}]}

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("find-items", str(request.url))
            self.assertIn("article=A001", str(request.url))
            return httpx.Response(200, json=payload)

        client = OnecClient("http://x/api", "token")
        client._client = httpx.Client(transport=httpx.MockTransport(handler),
                                      base_url="http://x/api")
        out = client.find_items(article="A001")
        self.assertEqual(out.total, 1)
        got = out.items[0]
        self.assertEqual((got.ref, got.tm_code, got.article), ("YO-77", "T9", "A001"))
        self.assertEqual((got.parent_ref, got.parent_name), ("F-9", "Снятые"))
        self.assertTrue(got.not_exported)


if __name__ == "__main__":
    unittest.main()
