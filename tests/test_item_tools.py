"""Инструменты правки справочника у агента (§19.2, §19.7, §19.8).

Главное здесь — ГЕЙТ. Модель не видит `set-items` и не может ничего записать: она собирает
предложение, код превращает его в операции и кладёт в `pending_proposal` с пометкой
`kind='items'`, а запись делает обработчик по кнопке админа. Тесты закрепляют именно это, а
не разметку текста.
"""
import json
import tempfile
import unittest
from pathlib import Path

from src.agent.pricing_tools import PricingTools, clear_nomenclature_cache
from src.bot.pricing_handlers import _format_items_result
from src.onec.client import (Folder, FolderTree, ItemProperty, Nomenclature, NomItem,
                             PropertyCatalog, PropertyDef, PropertyOption)
from src.price_tool import modes
from src.price_tool.item_broadcast import build_item_broadcast
from src.storage.pricing import PricingStore

TM = "000000325"


def nom_item(ref="YO-1", **kw) -> NomItem:
    base = dict(ref=ref, id="1",
                name="Виниловый ламинат Linderwood Quartz Адана", article="LQ-01",
                unit="м2", size="1219x228x4", product_type="Виниловый ламинат",
                collection="Quartz", parent="Quartz", collection_ref="YO-00078954",
                alt_units={"упак": 1.84}, purchase=None, retail=None, rrc=None,
                full_name="Виниловый ламинат Linderwood Quartz Адана",
                site_name="Адана", product_type_ref="000000002",
                collection_code="0004046", length_from=1219.0, length_to=1219.0,
                width_from=228.0, width_to=228.0, thickness=4.0,
                properties=(ItemProperty("Класс", "0000002", "43 класс", "0000017"),))
    base.update(kw)
    return NomItem(**base)


class FakeOnec:
    def __init__(self, items):
        self._items = items
        self.reads = 0
        self.hidden_asked: list[bool] = []
        self.written: list[list[dict]] = []

    def by_tm_all(self, tm_code, include_not_exported=False, **kw):
        self.reads += 1
        self.hidden_asked.append(include_not_exported)
        return Nomenclature(tm=tm_code, total=len(self._items), items=self._items)

    def folders(self, product_type=None, tm=None):
        return FolderTree(total=2, items=[
            Folder("YO-00002590", "Водостойкий ламинат", "00000000001", "type", 2,
                   False, False, "000000002", "", 0),
            Folder("YO-00078953", "Виниловый ламинат SPC Linderwood", "YO-00002590",
                   "tm", 3, False, False, "000000002", TM, 1.0),
        ])

    def properties_by_type(self, type_code, tm=None, property_code=None):
        return PropertyCatalog(
            product_type="Виниловый ламинат", product_type_ref="000000002",
            matched_folder={"code": "0004045", "name": "Linderwood"},
            properties=[PropertyDef("Коллекция", "0000003",
                                    [PropertyOption("Quartz", "0004046")]),
                        PropertyDef("Длина", "0000045", [])])

    def set_items(self, ops):
        self.written.append(ops)
        return {"date": "2026-09-09", "created": 1, "updated": 0, "unchanged": 0,
                "skipped": 0, "failed": 0, "results": [], "errors": []}


def proposal_input(**kw) -> dict:
    base = dict(tm_code=TM, tm_name="Linderwood", product_type="000000002",
                product_type_name="Виниловый ламинат", collection="QUARTZ",
                items=[{"op": "update", "ref": "YO-1", "article": "LQ-01",
                        "title": "Адана", "tail": "LQ-01", "pack_coefficient": 2.23}])
    base.update(kw)
    return base


class ToolsTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec([nom_item()])
        self.tools = PricingTools(self.onec, self.store, user_id=42)
        self.tools.mode = modes.ITEMS_PRICES

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_tools_are_registered(self):
        for name in ("get_1c_folders", "get_1c_properties", "propose_items"):
            self.assertTrue(self.tools.handles(name), name)

    async def test_folders_returns_kind_from_1c(self):
        out = json.loads(await self.tools.execute("get_1c_folders", {"tm": TM}))
        kinds = {f["ref"]: f["kind"] for f in out["folders"]}
        self.assertEqual(kinds["YO-00002590"], "type")
        self.assertEqual(kinds["YO-00078953"], "tm")

    async def test_properties_keep_empty_values(self):
        """«Длина» с пустым списком — это поле под число, а не отсутствующее свойство."""
        out = json.loads(await self.tools.execute("get_1c_properties",
                                                  {"product_type": "000000002"}))
        by_code = {p["code"]: p for p in out["properties"]}
        self.assertEqual(by_code["0000045"]["values"], [])
        self.assertEqual(out["matched_folder"]["code"], "0004045")

    async def test_proposal_is_saved_as_items_kind(self):
        await self.tools.execute("propose_items", proposal_input())
        pending = await self.store.get_pending(42)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.kind, "items")
        self.assertEqual(pending.payload[0]["op"], "update_item")
        self.assertEqual(pending.payload[0]["pack_coefficient"], 2.23)

    async def test_model_cannot_write(self):
        """Инструмент только сохраняет предложение — в 1С за него никто не ходит."""
        await self.tools.execute("propose_items", proposal_input())
        self.assertEqual(self.onec.written, [])

    async def test_prices_only_mode_refuses(self):
        self.tools.mode = modes.PRICES_ONLY
        text = await self.tools.execute("propose_items", proposal_input())
        self.assertIn("только цены", text)
        self.assertIsNone(await self.store.get_pending(42))

    async def test_nothing_to_change_saves_nothing(self):
        text = await self.tools.execute("propose_items", proposal_input(
            items=[{"op": "update", "ref": "YO-1", "article": "LQ-01",
                    "title": "Адана", "tail": "LQ-01", "thickness": 4}]))
        self.assertIn("Расхождений нет", text)
        self.assertIsNone(await self.store.get_pending(42))

    async def test_new_proposal_warns_that_the_old_button_is_dead(self):
        """Админ отвечает текстом, не нажимая кнопку, — агент предлагает заново (§19.7).

        Прежняя кнопка после этого не срабатывает, и узнать об этом админ должен из
        сообщения, а не по молчанию бота.
        """
        await self.tools.execute("propose_items", proposal_input())
        first = await self.store.get_pending(42)
        text = await self.tools.execute("propose_items", proposal_input(
            items=[{"op": "update", "ref": "YO-1", "article": "LQ-01",
                    "title": "Адана", "tail": "LQ-01", "pack_coefficient": 2.5}]))
        self.assertIn("Прежнее предложение", text)
        second = await self.store.get_pending(42)
        self.assertNotEqual(first.proposal_id, second.proposal_id)
        self.assertIsNone(await self.store.take_pending(42, first.proposal_id))

    async def test_no_warning_when_nothing_was_pending(self):
        text = await self.tools.execute("propose_items", proposal_input())
        self.assertNotIn("Прежнее предложение", text)

    async def test_missing_tm_code_refuses(self):
        text = await self.tools.execute("propose_items", proposal_input(tm_code=""))
        self.assertIn("tm_code", text)

    async def test_item_modes_ask_1c_for_discontinued(self):
        """Без снятых с производства агент заведёт дубль вместо возврата (§19.3)."""
        await self.tools.execute("propose_items", proposal_input())
        self.assertEqual(self.onec.hidden_asked, [True])

    async def test_price_mode_does_not_ask_for_discontinued(self):
        self.tools.mode = modes.PRICES_ONLY
        await self.tools.execute("get_1c_nomenclature", {"tm_code": TM})
        self.assertEqual(self.onec.hidden_asked, [False])

    async def test_one_read_per_mark_in_mixed_mode(self):
        """Товарная и ценовая ветки не должны читать одну марку дважды (§9.6.3)."""
        await self.tools.execute("get_1c_nomenclature", {"tm_code": TM})
        await self.tools.execute("propose_items", proposal_input())
        self.assertEqual(self.onec.reads, 1)


class NomenclatureFieldsTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.tools = PricingTools(FakeOnec([nom_item()]), self.store, user_id=42)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _one(self) -> dict:
        out = json.loads(await self.tools.execute("get_1c_nomenclature", {"tm_code": TM}))
        return out["items"][0]

    async def test_item_mode_includes_catalogue_fields(self):
        self.tools.mode = modes.ITEMS_ONLY
        item = await self._one()
        self.assertEqual(item["site_name"], "Адана")
        self.assertEqual(item["thickness"], 4.0)
        self.assertEqual(item["properties"][0]["value_code"], "0000017")

    async def test_price_mode_omits_them(self):
        """В ценовом режиме они не влияют ни на одно решение, а едут в каждый запрос."""
        self.tools.mode = modes.PRICES_ONLY
        item = await self._one()
        for key in ("site_name", "thickness", "properties", "not_exported"):
            self.assertNotIn(key, item)


class ReportTest(unittest.TestCase):

    def test_skipped_and_errors_are_visible(self):
        """«создано 0» без причины админ прочитает как «нечего было делать»."""
        text = _format_items_result({
            "date": "2026-09-09", "created": 0, "updated": 2, "skipped": 3, "failed": 1,
            "errors": [{"index": 4, "ref": "", "code": "skipped_dependency",
                        "message": "не создалась папка коллекции"}]})
        self.assertIn("изменено: 2", text)
        self.assertIn("пропущено: 3", text)
        self.assertIn("не создалась папка коллекции", text)

    def test_empty_result_says_so(self):
        text = _format_items_result({"date": "2026-09-09"})
        self.assertIn("нечего", text)


class DigestTest(unittest.IsolatedAsyncioTestCase):
    """Дайджест должен ложиться в готовый item_broadcast без переходников."""

    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.tools = PricingTools(FakeOnec([nom_item(), nom_item("YO-2")]),
                                  self.store, user_id=42)
        self.tools.mode = modes.ITEMS_PRICES

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_manager_sees_pack_change(self):
        await self.tools.execute("propose_items", proposal_input())
        digest = (await self.store.get_pending(42)).digest
        text = build_item_broadcast(digest, for_admin=False)
        self.assertIn("коэффициент упаковки", text)
        self.assertIn("1.84 → 2.23", text)

    async def test_normalization_hidden_from_managers_shown_to_admin(self):
        await self.tools.execute("propose_items", proposal_input(items=[
            {"op": "update", "ref": "YO-1", "article": "LQ-01", "title": "Адана",
             "tail": "LQ-01", "pack_coefficient": 2.23},
            {"op": "update", "ref": "YO-2", "article": "LQ-01", "title": "адана",
             "tail": "LQ-01"},
        ]))
        digest = (await self.store.get_pending(42)).digest
        self.assertNotIn("нормализация", build_item_broadcast(digest) or "")
        self.assertIn("нормализация", build_item_broadcast(digest, for_admin=True))

    async def test_size_change_shown_whole(self):
        """«толщина 3.5 → 4» без остальных чисел менеджеру ничего не даёт."""
        await self.tools.execute("propose_items", proposal_input(items=[
            {"op": "update", "ref": "YO-1", "article": "LQ-01", "title": "Адана",
             "tail": "LQ-01", "thickness": 5}]))
        digest = (await self.store.get_pending(42)).digest
        self.assertIn("1219x228x4 → 1219x228x5", build_item_broadcast(digest))


class PriceGateTest(unittest.IsolatedAsyncioTestCase):
    """§19.7: цены идут ПОСЛЕ правок товара и по ОДНОЙ коллекции.

    Раньше порядок держался на одном тексте промпта, и модель обошла его законным путём:
    `propose_prices` отказывал только при нескольких ТМ, а несколько коллекций одной марки
    принимал спокойно. На боевом прогоне 10.09.2026 агент проверил справочник одной
    коллекции Peli из шести — незаполненная фаска у 21 позиции осталась лежать, хотя он сам
    её и заметил.
    """

    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec([nom_item()])
        self.tools = PricingTools(self.onec, self.store, user_id=42)
        self.tools.mode = modes.ITEMS_PRICES

    async def asyncTearDown(self):
        self._dir.cleanup()

    def _prices(self, **kw) -> dict:
        group = {"tm_code": TM, "tm_name": "Linderwood",
                 "collection_ref": "YO-00078954", "collection": "Quartz",
                 "purchase": 1200, "rrc": 1800}
        group.update(kw)
        return {"supplier": "LINDERWOOD", "groups": [group]}

    async def test_prices_refused_before_items_check(self):
        text = await self.tools.execute("propose_prices", self._prices())
        self.assertIn("справочник ещё не проверялся", text)
        self.assertIsNone(await self.store.get_pending(42))

    async def test_prices_allowed_after_propose_items(self):
        await self.tools.execute("propose_items", proposal_input())
        await self.store.reject(42, (await self.store.get_pending(42)).proposal_id)
        text = await self.tools.execute("propose_prices", self._prices())
        self.assertNotIn("справочник ещё не проверялся", text)

    async def test_check_counts_even_when_nothing_to_fix(self):
        """«Сверил, править нечего» — такой же результат проверки, как список правок."""
        await self.tools.execute("propose_items", proposal_input(items=[]))
        self.assertIsNone(await self.store.get_pending(42))
        text = await self.tools.execute("propose_prices", self._prices())
        self.assertNotIn("справочник ещё не проверялся", text)

    async def test_several_collections_refused(self):
        payload = self._prices()
        payload["groups"].append({"tm_code": TM, "collection_ref": "YO-OTHER",
                                  "collection": "Vintage", "purchase": 900})
        text = await self.tools.execute("propose_prices", payload)
        self.assertIn("несколько коллекций", text)
        self.assertIsNone(await self.store.get_pending(42))

    async def test_collection_matched_by_name_ignoring_case(self):
        """«Elegance large» в 1С и «Elegance Large» в предложении — одна коллекция."""
        await self.store.mark_items_checked(42, ["elegance large"])
        text = await self.tools.execute("propose_prices",
                                        self._prices(collection="Elegance Large",
                                                     collection_ref=""))
        self.assertNotIn("справочник ещё не проверялся", text)

    async def test_gate_is_off_in_prices_only_mode(self):
        """В режиме «только цены» справочник не трогаем вовсе — запирать нечем."""
        self.tools.mode = modes.PRICES_ONLY
        text = await self.tools.execute("propose_prices", self._prices())
        self.assertNotIn("справочник ещё не проверялся", text)

    async def test_new_price_list_clears_the_marks(self):
        """Отметки живут в рамках одного прайса: новый файл — новая проверка."""
        await self.tools.execute("propose_items", proposal_input(items=[]))
        await self.store.reset(42)
        text = await self.tools.execute("propose_prices", self._prices())
        self.assertIn("справочник ещё не проверялся", text)


if __name__ == "__main__":
    unittest.main()
