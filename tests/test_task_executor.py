"""Выполнение задачи с настоящей записью в 1С (§6.2 specs/agent-workflow-model.md).

Клиент 1С и оркестратор поддельные: проверяется не то, что 1С поймёт payload — это
проверяет прайсовый поток, — а обвязка вокруг записи, которую живой базой как раз
проверить нельзя без порчи данных.

ГЛАВНОЕ ЗДЕСЬ — ЗАЩИТА ЗАПИСИ. Проверка права стоит вплотную перед вызовом 1С, потому что
необратима именно запись: статус мы поправим, а цены в справочнике нет.
"""
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from src.model.enums import TaskKind, TaskStatus
from src.model.offers import Offer
from src.model.events import Broadcaster
from src.model.executor import TaskTools, WriteRefused, run, task_brief
from src.model.price import Price, SupplierPrice
from src.model.refs import Ref, TaskAddress
from src.model.task import PriceTask
from src.onec.client import NomItem, Price as OnecPrice
from src.storage import price_files
from src.storage.model_store import ModelStore
from src.storage.suppliers import SupplierStore


def nom(ref="T1", article="A1", name="Дуб Верона", collection="Vintage",
        purchase="1000", unit="м2"):
    return NomItem(
        ref=ref, id="", name=name, article=article, unit=unit, size="",
        product_type="Ламинат", collection=collection, parent=collection,
        collection_ref="F1", alt_units={},
        purchase=OnecPrice(value=float(purchase), date=None),
        retail=None, rrc=None, product_type_ref="PT1")


class FakeNomenclature:
    def __init__(self, items, tm=""):
        self.items = list(items)
        self.tm = tm
        self.errors = []


class FakeOnec:
    """Считает записи и отдаёт заданную номенклатуру."""

    def __init__(self, items=None, tm_name="Most Flooring"):
        self._items = list(items or [nom()])
        self.tm_name = tm_name
        self.price_writes = []
        self.item_writes = []

    def by_tm_all(self, tm_code, **kw):
        return FakeNomenclature(self._items, tm=self.tm_name)

    def set_prices(self, payload):
        self.price_writes.append(payload)
        return {"updated": len(payload), "unchanged": 0, "errors": []}

    def set_items(self, ops):
        self.item_writes.append(ops)
        return {"created": 0, "updated": len(ops), "errors": []}


def make_task(kind=TaskKind.CHANGE_PRICES, description="обновить цены"):
    return PriceTask(kind=kind,
                     address=TaskAddress(tm=Ref.make(code="TM1", names=["Egger"]),
                                         subject=Ref.make(names=["Vintage"])),
                     description=description, id=7)


def make_price():
    return Price(supplier_price=SupplierPrice(supplier_id=1, file_id=1,
                                              file_path="p.xlsx",
                                              filename="Прайс.xlsx"), id=3)


def allow():
    """Право на запись есть."""
    return None


def deny():
    raise WriteRefused("захват потерян")


class FakeOffers:
    """Журнал предложений: артикул → что просят ДРУГИЕ поставщики."""

    def __init__(self, table=None):
        self._table = table or {}
        self.asked = []

    async def offers(self, keys, exclude_supplier_id=0):
        self.asked.append((list(keys), exclude_supplier_id))
        return {k: v for k, v in self._table.items() if k in set(keys)}


class CheapestPriceTest(unittest.IsolatedAsyncioTestCase):
    """Наименьшая АКТУАЛЬНАЯ цена (§6.4).

    Вопрос админа 22.09.2026: один товар возят несколько поставщиков, и прайс подороже
    не должен затирать цену подешевле. Решает КОД — модель о конкурентах не знает.
    """

    def tools(self, onec, table, price_date="2026-09-22"):
        return TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PRICES,
                         offers=FakeOffers(table), supplier_id=1, price_date=price_date)

    async def test_cheaper_fresh_offer_wins(self):
        onec = FakeOnec([nom(ref="T1", article="A1", purchase="2000")])
        tools = self.tools(onec, {"a1": [Offer(2, "Паркет-Холл", 1560,
                                               price_date="2026-09-15")]})
        await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1880})

        written = onec.price_writes[0][0]["prices"]
        self.assertEqual(written["purchase"], 1560)
        self.assertIn("Паркет-Холл", " ".join(tools.price_notes))

    async def test_stale_offer_does_not_block_the_write(self):
        """Цена годовой давности про сегодня не говорит ничего — пишем свежую."""
        onec = FakeOnec([nom(ref="T1", article="A1", purchase="2000")])
        tools = self.tools(onec, {"a1": [Offer(2, "Паркет-Холл", 1560,
                                               price_date="2025-09-15")]})
        await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1880})

        written = onec.price_writes[0][0]["prices"]
        self.assertEqual(written["purchase"], 1880)
        # но админу об этом сказано: повод запросить свежий прайс
        self.assertIn("запросить свежий", " ".join(tools.price_notes))

    async def test_rrc_comes_from_the_winner(self):
        """Решение админа: пара «закупка + РРЦ» берётся у одного поставщика."""
        onec = FakeOnec([nom(ref="T1", article="A1", purchase="2000")])
        tools = self.tools(onec, {"a1": [Offer(2, "Паркет-Холл", 1560, rrc=2870,
                                               price_date="2026-09-15")]})
        await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1880, "rrc": 2980})

        written = onec.price_writes[0][0]["prices"]
        self.assertEqual(written["rrc"], 2870)

    async def test_article_shared_by_two_items_skips_the_competition(self):
        """ВОПРОС АДМИНА 25.09.2026. Один код декора бывает у двух товаров марки в разных
        толщинах: «6006-4» это и Spark (4 мм), и Modern (3,6 мм). Журнал предложений
        ключуется артикулом — другого межпоставщицкого ключа нет, — и чужая цена по
        такому коду неизвестно про какую толщину. Приняв её, мы увезли бы цену 3,6 мм на
        4 мм. Свою цену из прайса при этом пишем как обычно."""
        onec = FakeOnec([nom(ref="T1", article="6006-4", purchase="2000"),
                         nom(ref="T2", article="6006-4", collection="Modern",
                             purchase="960")])
        tools = self.tools(onec, {"60064": [Offer(2, "Паркет-Холл", 900,
                                                  price_date="2026-09-15")]})
        await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1880})

        written = onec.price_writes[0][0]["prices"]
        self.assertEqual(written["purchase"], 1880, "чужое предложение не принято")
        notes = " ".join(tools.price_notes)
        self.assertIn("нескольких товаров марки", notes)
        self.assertNotIn("Паркет-Холл", notes)

    async def test_item_out_of_competition_still_gets_its_price(self):
        """Из КОНКУРЕНЦИИ такая позиция выбывает, а из ЗАПИСИ — нет: иначе товар остался
        бы вообще без цены, а это хуже того, от чего защищаемся."""
        onec = FakeOnec([nom(ref="T1", article="6006-4", purchase="2000"),
                         nom(ref="T2", article="6006-4", collection="Modern",
                             purchase="960"),
                         nom(ref="T3", article="UNIQ", purchase="2000")])
        tools = self.tools(onec, {"uniq": [Offer(2, "Паркет-Холл", 1500,
                                                 price_date="2026-09-15")]})
        await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1880})

        written = {p["ref"]: p["prices"]["purchase"]
                   for p in onec.price_writes[0] if "ref" in p}
        self.assertEqual(written.get("T3"), 1500, "по уникальному коду конкурент выиграл")
        self.assertEqual(written.get("T1"), 1880, "а эта позиция получила цену прайса")

    async def test_skipped_price_kind_is_named_in_the_report(self):
        """СЛУЧАЙ С БОЯ (25.09.2026). У Spark в карточках стояла РРЦ 1850, в прайсе
        пришла 1870 — отличие 1,1%, меньше порога 2%, и код её законно пропустил. Закупка
        и розница изменились, админ увидел «цены записаны» и решил, что РРЦ потерялась.
        Правило верное, отчёт был неполным."""
        from src.onec.client import Price as OnecPrice

        item = nom(ref="T1", article="A1", purchase="1500")
        item = item.__class__(**{**item.__dict__,
                                 "rrc": OnecPrice(value=1850.0, date=None)})
        onec = FakeOnec([item])
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PRICES)
        out = await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1150, "rrc": 1870})

        self.assertIn("Пропущено", out)
        self.assertIn("РРЦ", out)
        self.assertIn("1850", out)

    async def test_without_the_journal_nothing_changes(self):
        """Журнала нет — пишем то, что дал прайс, и групповой формой, как раньше."""
        onec = FakeOnec([nom(ref="T1", article="A1", purchase="2000")])
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PRICES)
        await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1880})

        sent = onec.price_writes[0][0]
        self.assertEqual(sent["prices"]["purchase"], 1880)
        self.assertIn("collection_ref", sent)      # групповая форма записи сохранилась

    async def test_source_of_the_price_goes_to_1c(self):
        """В 1С уезжает, ЧЕЙ прайс дал цену: там заводится зеркало справочника
        поставщиков, и по нему видно, у кого закупаем (§6.4)."""
        onec = FakeOnec([nom(ref="T1", article="A1", purchase="2000")])
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PRICES,
                          supplier_id=7, supplier_name="Монарх")
        await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1880})

        source = onec.price_writes[0][0]["source"]
        self.assertEqual(source["supplier_code"], "7")
        self.assertEqual(source["supplier"], "Монарх")

    async def test_source_names_the_winner_not_us(self):
        """Победил чужой прайс — в 1С уедет ЕГО имя, иначе атрибуция соврёт."""
        onec = FakeOnec([nom(ref="T1", article="A1", purchase="2000")])
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PRICES,
                          offers=FakeOffers({"a1": [Offer(2, "Паркет-Холл", 1560,
                                                          price_date="2026-09-15")]}),
                          supplier_id=1, supplier_name="Монарх",
                          price_date="2026-09-22")
        await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1880})

        source = onec.price_writes[0][0]["source"]
        self.assertEqual(source["supplier_code"], "2")
        self.assertEqual(source["supplier"], "Паркет-Холл")

    async def test_our_own_price_wins_when_it_is_lowest(self):
        onec = FakeOnec([nom(ref="T1", article="A1", purchase="2000")])
        tools = self.tools(onec, {"a1": [Offer(2, "Паркет-Холл", 1900,
                                               price_date="2026-09-15")]})
        await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1880})

        self.assertEqual(onec.price_writes[0][0]["prices"]["purchase"], 1880)
        self.assertEqual(tools.price_notes, [])


class WriteGuardTest(unittest.IsolatedAsyncioTestCase):
    """Никакая запись не проходит без свежей проверки права."""

    async def test_prices_are_written_when_allowed(self):
        onec = FakeOnec()
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PRICES)
        out = await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1500})
        self.assertTrue(onec.price_writes, "цены обязаны уйти в 1С: %s" % out)
        self.assertEqual(tools.written_prices, 1)

    async def test_rrc_reaches_1c_together_with_the_purchase(self):
        """СЛУЧАЙ С БОЯ (22.09.2026). По «Классик» агент записал закупку и доложил: «РРЦ
        2980 отдельным полем не записывалась — инструмент цен принимает только
        закупочную». Планировщик РРЦ умел всегда, не хватало входа, и круг не закрывался:
        следующая сверка снова показала бы расхождение, а чинить его было нечем."""
        onec = FakeOnec()
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PRICES)
        await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage",
            "purchase": 1500, "rrc": 2400})

        sent = onec.price_writes[0]
        prices = sent[0]["prices"] if isinstance(sent, list) else sent["prices"]
        self.assertEqual(prices.get("purchase"), 1500)
        self.assertEqual(prices.get("rrc"), 2400)

    async def test_prices_are_not_written_when_refused(self):
        onec = FakeOnec()
        tools = TaskTools(onec, b"", "p.xlsx", deny, kind=TaskKind.CHANGE_PRICES)
        out = await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1500})
        self.assertEqual(onec.price_writes, [], "запись прошла без права")
        self.assertIn("ЗАПИСЬ ОТМЕНЕНА", out)

    async def test_items_are_not_written_when_refused(self):
        onec = FakeOnec()
        tools = TaskTools(onec, b"", "p.xlsx", deny, kind=TaskKind.ADD_NEW)
        await tools.execute("write_items", {
            "tm_code": "TM1", "product_type": "PT1", "collection": "Vintage",
            "items": [{"op": "update", "ref": "T1", "title": "Дуб Медовый"}]})
        self.assertEqual(onec.item_writes, [], "запись прошла без права")

    async def test_guard_is_checked_after_the_plan_not_before(self):
        """Право проверяется ВПЛОТНУЮ перед записью.

        Сборка плана ходит в 1С за выгрузкой и занимает время; проверив право до неё, мы
        бы разрешили запись по праву, которого к моменту записи уже нет.
        """
        onec = FakeOnec()
        seen = []

        def watching():
            seen.append(len(onec.price_writes))

        tools = TaskTools(onec, b"", "p.xlsx", watching, kind=TaskKind.CHANGE_PRICES)
        await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1500})
        # проверка была ровно одна и ДО записи
        self.assertEqual(seen, [0])
        self.assertEqual(len(onec.price_writes), 1)

    async def test_nothing_to_write_does_not_touch_1c(self):
        """Совпадающая цена не должна порождать запись: 2% порог держит `plan_collection`."""
        onec = FakeOnec()
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PRICES)
        out = await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Vintage", "purchase": 1000})
        self.assertEqual(onec.price_writes, [])
        self.assertIn("Изменений нет", out)

    async def test_unknown_collection_is_refused_not_guessed(self):
        onec = FakeOnec()
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PRICES)
        out = await tools.execute("write_prices", {
            "tm_code": "TM1", "collection": "Такой нет", "purchase": 1500})
        self.assertEqual(onec.price_writes, [])
        self.assertIn("нет коллекции", out)


class BrandNameTest(unittest.IsolatedAsyncioTestCase):
    """Имя марки в наименованиях берётся из 1С, а не из того, что прислала модель.

    ЧТО ЭТО ЛОВИТ. `build_name` собирает имя из частей, и марка была среди них. Модель,
    увидев в прайсе «MOST FLOOR», честно передавала это написание — и нормализация
    переименовывала сотни позиций. Запретом в промпте не лечится: модель копирует не по
    злому умыслу, а потому что так написано в источнике.
    """

    async def _written_name(self, onec, tm_name):
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.NORMALIZE_NAMES)
        await tools.execute("write_items", {
            "tm_code": "TM1", "tm_name": tm_name,
            "product_type": "PT1", "product_type_name": "Ламинат",
            "collection": "Vintage",
            "items": [{"op": "update", "ref": "T1", "title": "Дуб Медовый"}]})
        if not onec.item_writes:
            return ""
        ops = onec.item_writes[0]
        return next((o.get("name", "") for o in ops if o.get("name")), "")

    async def test_the_1c_spelling_wins(self):
        onec = FakeOnec(tm_name="Most Flooring")
        name = await self._written_name(onec, "MOST FLOOR")
        self.assertIn("Most Flooring", name)
        self.assertNotIn("MOST FLOOR", name)

    async def test_divergence_is_reported_not_swallowed(self):
        """Переименование марки — решение админа, и он узнает о расхождении только так."""
        onec = FakeOnec(tm_name="Most Flooring")
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.NORMALIZE_NAMES)
        out = await tools.execute("write_items", {
            "tm_code": "TM1", "tm_name": "MOST FLOOR",
            "product_type": "PT1", "product_type_name": "Ламинат",
            "collection": "Vintage",
            "items": [{"op": "update", "ref": "T1", "title": "Дуб Медовый"}]})
        self.assertIn("Most Flooring", out)
        self.assertIn("MOST FLOOR", out)

    async def test_matching_spelling_says_nothing(self):
        onec = FakeOnec(tm_name="Most Flooring")
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.NORMALIZE_NAMES)
        out = await tools.execute("write_items", {
            "tm_code": "TM1", "tm_name": "most flooring",
            "product_type": "PT1", "product_type_name": "Ламинат",
            "collection": "Vintage",
            "items": [{"op": "update", "ref": "T1", "title": "Дуб Медовый"}]})
        self.assertNotIn("⚠️ Марка", out)


class CollectionPropertyTest(unittest.IsolatedAsyncioTestCase):
    """Модель не может заполнить свойство «Коллекция», даже если попытается.

    Запрет стоит у самой записи, а не только в промпте: выгрузка отдаёт коллекцию уже
    ВЫВЕДЕННОЙ (из имени папки, когда реквизит пуст), и модель, добросовестно увидев её,
    возвращает то же значение свойством. Имя папки для этого не годится — оно несёт
    размер и меняется при пересортировке справочника.
    """

    async def test_property_is_dropped_before_the_write(self):
        from src.model.normalize import COLLECTION_PROPERTY
        onec = FakeOnec()
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PROPERTIES)
        out = await tools.execute("write_items", {
            "tm_code": "TM1", "product_type": "PT1", "product_type_name": "Ламинат",
            "collection": "Vintage",
            "items": [{"op": "update", "ref": "T1", "title": "Дуб Медовый",
                       "properties": [{"property": COLLECTION_PROPERTY,
                                       "value_code": "V1"}]}]})
        self.assertIn("Коллекция", out)
        written = json.dumps(onec.item_writes, ensure_ascii=False)
        self.assertNotIn(COLLECTION_PROPERTY, written)

    async def test_tool_description_states_the_rule(self):
        from src.model.executor import TOOLS
        write = next(t for t in TOOLS if t["name"] == "write_items")
        # Правило стало точнее: раньше свойство было запрещено вовсе, теперь модель может
        # ПОПРОСИТЬ его проставить, но значение выбирает код по имени коллекции.
        self.assertIn("САМ НЕ ЗАПОЛНЯЙ", write["description"])
        self.assertIn("set_collection_property", write["description"])


class NewCollectionTest(unittest.IsolatedAsyncioTestCase):
    """Создание новой коллекции: имя папки, значение свойства, цены следом.

    СЛУЧАЙ С БОЯ (21.09.2026, «Классик» у A+ Floor). Три ошибки разом: папка получила
    размер дважды, свойство «Коллекция» не завелось вовсе, а `write_prices` потом не
    нашёл только что созданную коллекцию и цены остались непроставленными.
    """

    def payload(self, folder_name="Классик 600x238x12"):
        return {
            "tm_code": "TM1", "tm_name": "A+ Floor",
            "product_type": "000000003", "product_type_name": "Ламинат",
            "collection": "Классик",
            "new_folder": {"parent_ref": "F-TM", "name": folder_name},
            "items": [{"op": "create", "ref": "", "article": "301",
                       "title": "Аристо", "length": 600, "width": 238,
                       "thickness": 12}],
        }

    def onec(self):
        class Fake(FakeOnec):
            def __init__(self):
                super().__init__([], tm_name="A+ Floor")
                self.batches = []

            def set_items(self, ops):
                self.batches.append(ops)
                if ops and ops[0].get("op") == "add_property_value":
                    return {"results": [{"op": "add_property_value",
                                         "ref": "V-777", "status": "created"}],
                            "created": 1, "updated": 0, "errors": []}
                return {"created": len(ops), "updated": 0, "errors": []}

        return Fake()

    async def run_write(self, onec, folder_name="Классик 600x238x12"):
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.ADD_NEW)
        return await tools.execute("write_items", self.payload(folder_name))

    async def test_size_is_not_doubled_in_the_folder_name(self):
        """Модель видит в 1С папки вида «Ле Паркет 600x600x14» и повторяет образец,
        передавая имя УЖЕ с размером. Дописав свой, получаем размер дважды."""
        onec = self.onec()
        await self.run_write(onec)
        folder = next(o for b in onec.batches for o in b
                      if o.get("op") == "create_folder")
        self.assertEqual(folder["name"].count("600x238x12"), 1, folder["name"])

    async def test_name_without_a_size_still_gets_one(self):
        onec = self.onec()
        await self.run_write(onec, folder_name="Классик")
        folder = next(o for b in onec.batches for o in b
                      if o.get("op") == "create_folder")
        self.assertIn("600x238x12", folder["name"])

    async def test_collection_property_value_is_created_and_attached(self):
        """Без значения свойства позиция уходит на сайт без признака коллекции."""
        from src.model.normalize import COLLECTION_PROPERTY
        onec = self.onec()
        await self.run_write(onec)

        first = onec.batches[0][0]
        self.assertEqual(first["op"], "add_property_value")
        self.assertEqual(first["value"], "Классик")
        # значение кладётся в папку МАРКИ, иначе теряется среди трёх тысяч в корне
        self.assertEqual(first["folder_name"], "A+ Floor")

        created = next(o for o in onec.batches[1] if o.get("op") == "create_item")
        self.assertIn({"property": COLLECTION_PROPERTY, "value_code": "V-777"},
                      created["properties"])

    async def test_existing_items_get_the_property_on_request(self):
        """Свойство «Коллекция» у восьми позиций «Классик» не завелось вовсе, и заполнить
        его было нечем: код отбрасывал любое значение от модели. Теперь модель может
        ПОПРОСИТЬ, а значение выбирает код по имени коллекции."""
        from src.model.normalize import COLLECTION_PROPERTY
        onec = self.onec()
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PROPERTIES)
        await tools.execute("write_items", {
            "tm_code": "TM1", "tm_name": "A+ Floor",
            "product_type": "000000003", "product_type_name": "Ламинат",
            "collection": "Классик", "set_collection_property": True,
            "items": [{"op": "update", "ref": "T1", "title": "Аристо"}]})

        self.assertEqual(onec.batches[0][0]["op"], "add_property_value")
        updated = next(o for o in onec.batches[1] if o.get("op") == "update_item")
        self.assertIn({"property": COLLECTION_PROPERTY, "value_code": "V-777"},
                      updated["properties"])

    async def test_property_is_set_even_when_nothing_else_changes(self):
        """СЛУЧАЙ С БОЯ («Натур» у A+ Floor). Имена были в порядке, `plan_collection`
        правок не нашёл — и метод выходил на «расхождений нет» ещё ДО того, как дело
        доходило до свойства. Задача честно докладывала «изменений в 1С нет».

        Проставить свойство — само по себе работа, даже когда больше менять нечего.
        """
        from src.model.normalize import COLLECTION_PROPERTY
        empty = nom(ref="T1", article="A1", collection="")
        object.__setattr__(empty, "parent", "Натур")
        onec = self.onec()
        onec._items = [empty]

        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PROPERTIES)
        out = await tools.execute("write_items", {
            "tm_code": "TM1", "tm_name": "A+ Floor",
            "product_type": "000000003", "product_type_name": "Ламинат",
            "collection": "Натур", "set_collection_property": True,
            "items": [],                      # модель ничего не правит — только просит
        })

        updates = [o for o in onec.batches[1] if o.get("op") == "update_item"]
        self.assertEqual(len(updates), 1)
        self.assertIn({"property": COLLECTION_PROPERTY, "value_code": "V-777"},
                      updates[0]["properties"])
        # и отчёт не противоречит сам себе
        self.assertIn("проставлено", out)

    async def test_items_that_already_have_it_are_not_touched(self):
        filled = nom(ref="T1", article="A1", collection="Натур")
        onec = self.onec()
        onec._items = [filled]

        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PROPERTIES)
        await tools.execute("write_items", {
            "tm_code": "TM1", "tm_name": "A+ Floor",
            "product_type": "000000003", "product_type_name": "Ламинат",
            "collection": "Натур", "set_collection_property": True, "items": []})
        self.assertEqual(onec.batches, [])

    def test_tool_says_the_property_is_absent_from_the_type_list(self):
        """Агент, не найдя «Коллекцию» в свойствах вида товара, заключил, что её надо
        заводить в конфигурации. Она там и не должна быть — свойство общее."""
        from src.model.executor import TOOLS
        props = next(t for t in TOOLS if t["name"] == "get_1c_properties")
        self.assertIn("«Коллекции» в этом списке НЕТ", props["description"])

    async def test_the_model_cannot_choose_the_value(self):
        """Просить можно, выбирать значение — нет: запрет «не заполнять именем папки»
        держится тем, что код берёт имя коллекции, а не то, что прислала модель."""
        from src.model.normalize import COLLECTION_PROPERTY
        onec = self.onec()
        tools = TaskTools(onec, b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PROPERTIES)
        await tools.execute("write_items", {
            "tm_code": "TM1", "tm_name": "A+ Floor",
            "product_type": "000000003", "product_type_name": "Ламинат",
            "collection": "Классик", "set_collection_property": True,
            "items": [{"op": "update", "ref": "T1", "title": "Аристо",
                       "properties": [{"property": COLLECTION_PROPERTY,
                                       "value_code": "ЧУЖОЙ"}]}]})
        written = json.dumps(onec.batches, ensure_ascii=False)
        self.assertNotIn("ЧУЖОЙ", written)
        self.assertIn("V-777", written)

    async def test_items_are_still_created_if_the_value_fails(self):
        """Карточка без свойства лучше, чем её отсутствие: свойство админ проставит."""
        class Broken(FakeOnec):
            def __init__(self):
                super().__init__([], tm_name="A+ Floor")
                self.batches = []

            def set_items(self, ops):
                self.batches.append(ops)
                if ops[0].get("op") == "add_property_value":
                    return {"results": [], "errors": [{"code": "x", "message": "нет"}]}
                return {"created": len(ops), "updated": 0, "errors": []}

        onec = Broken()
        out = await self.run_write(onec)
        self.assertTrue(any(o.get("op") == "create_item" for o in onec.batches[1]))
        self.assertIn("завести не удалось", out)


class DiscontinuedAreHiddenTest(unittest.IsolatedAsyncioTestCase):
    """Снятые не попадают в живую выгрузку — агенту про них знать незачем.

    РЕШЕНИЕ АДМИНА (21.09.2026). Часть коллекций висит под маркой с флагом «Не
    выгружать»: они уже сняты, а в папки снятых их перенесут отдельно и не сейчас. Видя
    их, агент начинал предлагать по ним работу — у Most Flooring так всплыли Quick и
    Prestige в отчёте по совсем другой задаче.
    """

    def mixed(self):
        live = nom(ref="R1", article="3309", name="Ламинат Egger Vintage Дуб")
        dead = nom(ref="R2", article="9001", name="Ламинат Egger Quick Ясень")
        object.__setattr__(dead, "not_exported", True)
        object.__setattr__(dead, "collection", "Quick")
        object.__setattr__(dead, "parent", "Quick")
        return [live, dead]

    async def test_discontinued_are_not_returned(self):
        tools = TaskTools(FakeOnec(self.mixed()), b"", "p.xlsx", allow,
                          kind=TaskKind.ADD_NEW)
        out = await tools.execute("get_1c_items", {"tm_code": "TM1"})
        self.assertIn("3309", out)
        self.assertNotIn("9001", out)

    async def test_the_cache_still_has_them(self):
        """Сборке правок снятые НУЖНЫ: без них создание позиции не увидит, что товар уже
        есть, и заведёт дубль. Поэтому фильтруется выдача, а не кеш."""
        tools = TaskTools(FakeOnec(self.mixed()), b"", "p.xlsx", allow,
                          kind=TaskKind.ADD_NEW)
        await tools.execute("get_1c_items", {"tm_code": "TM1"})
        cached = await tools._nomenclature("TM1")
        self.assertEqual(len(cached), 2)

    async def test_empty_answer_points_at_find_items(self):
        dead = nom(ref="R2", article="9001")
        object.__setattr__(dead, "not_exported", True)
        tools = TaskTools(FakeOnec([dead]), b"", "p.xlsx", allow, kind=TaskKind.ADD_NEW)
        out = await tools.execute("get_1c_items", {"tm_code": "TM1"})
        self.assertIn("find_1c_items", out)

    def test_prompt_forbids_discussing_them(self):
        from src.model.executor import PROMPT
        self.assertIn("СНЯТОЕ НЕ ТРОГАЙ И НЕ ОБСУЖДАЙ", PROMPT)


class OrchestratorContractTest(unittest.IsolatedAsyncioTestCase):
    """Исполнитель обязан отвечать на то, о чём его спрашивает оркестратор.

    СЛУЧАЙ С БОЯ (21.09.2026). `handle_turn` перед каждым инструментом зовёт
    `extra_executor.handles(name)`. Метода не было — `AttributeError` рушил ВЕСЬ прогон,
    а не отдельный вызов, и задача «перенос в снятые» вернула «прогон сорвался». Все
    инструменты по отдельности при этом работали, поэтому искать было негде.
    """

    def test_own_tools_are_recognised(self):
        from src.model.executor import TOOLS
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow)
        for tool in TOOLS:
            self.assertTrue(tools.handles(tool["name"]), tool["name"])

    def test_foreign_tools_are_declined(self):
        """Чужой инструмент должен уйти менеджерскому исполнителю, а не сюда."""
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow)
        self.assertFalse(tools.handles("search_emails"))
        self.assertFalse(tools.handles("read_price_file"))

    async def test_the_orchestrator_contract_is_satisfied(self):
        """Проверка ровно тем вызовом, которым падало: `handles` + `execute`."""
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow)
        name = "get_1c_items"
        self.assertTrue(tools.handles(name))
        self.assertTrue(await tools.execute(name, {"tm_code": "TM1"}))


class OutcomeTest(unittest.IsolatedAsyncioTestCase):

    async def test_finish_records_the_outcome(self):
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow)
        await tools.execute("finish", {"status": "выполнена",
                                       "result": "Записано 12 цен."})
        self.assertEqual(tools.outcome, (TaskStatus.DONE, "Записано 12 цен."))

    async def test_partial_without_a_reason_is_refused(self):
        """«Частично» без причины — бесполезный ответ: админ не узнает, что доделывать."""
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow)
        out = await tools.execute("finish", {"status": "частично обработана",
                                             "result": "не всё"})
        self.assertIsNone(tools.outcome)
        self.assertIn("требует причины", out)

    async def test_empty_result_is_refused(self):
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow)
        await tools.execute("finish", {"status": "выполнена", "result": "   "})
        self.assertIsNone(tools.outcome)

    async def test_unknown_status_is_refused_with_the_list(self):
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow)
        out = await tools.execute("finish", {"status": "готово", "result": "всё ок"})
        self.assertIsNone(tools.outcome)
        self.assertIn("к обработке", out)


class SilentAgentTest(unittest.IsolatedAsyncioTestCase):
    """Молчаливый агент не имеет права выдать себя за успех."""

    class Orchestrator:
        def __init__(self, calls=()):
            self._calls = list(calls)

        async def handle_turn(self, history, system=None, extra_tools=None,
                              extra_executor=None, **kw):
            for name, payload in self._calls:
                await extra_executor.execute(name, payload)
            return "молчу", history

    async def test_no_finish_and_no_write_leaves_the_task_open(self):
        status, result = await run(self.Orchestrator(), FakeOnec(), make_price(),
                                   make_task(), b"", allow)
        self.assertEqual(status, TaskStatus.TODO)
        self.assertIn("не доложил исход", result)

    async def test_no_finish_but_a_write_is_partial_not_open(self):
        """Прятать состоявшуюся запись за «к обработке» нельзя: админ повторит задачу и
        запишет второй раз."""
        onec = FakeOnec()
        orc = self.Orchestrator([
            ("write_prices", {"tm_code": "TM1", "collection": "Vintage",
                              "purchase": 1500}),
        ])
        status, result = await run(orc, onec, make_price(), make_task(), b"", allow)
        self.assertEqual(status, TaskStatus.PARTIAL)
        self.assertIn("запись в 1С состоялась", result)
        self.assertTrue(onec.price_writes)

    async def test_finish_wins_over_the_fallback(self):
        orc = self.Orchestrator([
            ("finish", {"status": "выполнена", "result": "Обновлено 5 цен в коллекции."}),
        ])
        status, result = await run(orc, FakeOnec(), make_price(), make_task(), b"", allow)
        self.assertEqual(status, TaskStatus.DONE)
        self.assertEqual(result, "Обновлено 5 цен в коллекции.")


class AdminEditTest(unittest.TestCase):
    """Правка админа старше рассуждений агента, и он должен это знать.

    Описание уезжает одним куском, и отличить «анализ агента» от «ответа админа» по
    структуре нельзя — поля разные у них нет. Значит правило должно быть в промпте:
    иначе агент читает свой же вопрос, не замечает ответа ниже и решает заново сам.
    """

    def test_prompt_names_the_admin_edit_authoritative(self):
        from src.model.executor import PROMPT
        self.assertIn("его слова старше твоих", PROMPT)
        self.assertIn("действуй по ОТВЕТУ", PROMPT)

    def test_prompt_says_he_cannot_ask_mid_run(self):
        """Ключевое ограничение: спросить посреди работы нельзя, только `finish`."""
        from src.model.executor import PROMPT
        self.assertIn("Переспросить посреди работы ты не можешь", PROMPT)


class BriefTest(unittest.TestCase):

    def test_brief_carries_what_the_agent_needs(self):
        task = make_task(description="сверить РРЦ")
        brief = task_brief(make_price(), task)
        self.assertIn("изменение цен", brief)
        self.assertIn("сверить РРЦ", brief)
        self.assertIn("TM1", brief)
        self.assertIn("Прайс.xlsx", brief)

    def test_previous_result_is_included_on_a_rerun(self):
        """Задачу запускают повторно именно тогда, когда в прошлый раз получилось не всё:
        агент должен доделать остаток, а не начать сначала."""
        task = make_task()
        task.complete(TaskStatus.PARTIAL, "12 из 15 записано, по трём нет позиций")
        self.assertIn("12 из 15", task_brief(make_price(), task))


class PayloadSizeTest(unittest.IsolatedAsyncioTestCase):
    """Объём выгрузки зависит от вида задачи — история едет в каждый следующий запрос."""

    async def test_price_task_gets_no_catalog_fields(self):
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow, kind=TaskKind.CHANGE_PRICES)
        out = await tools.execute("get_1c_items", {"tm_code": "TM1"})
        self.assertIn("purchase", out)
        self.assertNotIn("site_name", out)
        self.assertNotIn("properties", out)

    async def test_catalog_task_gets_them(self):
        tools = TaskTools(FakeOnec(), b"", "p.xlsx", allow,
                          kind=TaskKind.NORMALIZE_NAMES)
        out = await tools.execute("get_1c_items", {"tm_code": "TM1"})
        self.assertIn("site_name", out)
        self.assertIn("not_exported", out)


class ServiceWiringTest(unittest.IsolatedAsyncioTestCase):
    """Модель зовёт исполнитель и сохраняет его исход, а не свой."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        db = Path(self._dir.name) / "t.db"
        self.store = ModelStore(db)
        await self.store.init()
        self.suppliers = SupplierStore(db)
        await self.suppliers.init()
        self.db = db

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def build(self, run_task):
        from src.model.service import PriceListService
        model = PriceListService(
            self.store, self.suppliers,
            save_file=lambda c, n: price_files.save(self.db, n, c),
            broadcaster=Broadcaster(), run_task=run_task)
        await model.load()
        return model

    async def test_outcome_from_the_agent_is_stored(self):
        seen = {}

        async def runner(price, task, content, guard):
            seen["task"] = task.id
            return TaskStatus.PARTIAL, "Записано 3 из 5, по двум нет артикула."

        model = await self.build(runner)
        from tests.test_task_builder import workbook
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")
        price = model.prices[0]
        task = price.sorted_tasks[0]

        from src.model.commands import Command, CommandKind
        await model.apply(Command(kind=CommandKind.EXECUTE_TASK, actor="admin-1",
                                  price_id=price.id, task_id=task.id))

        self.assertEqual(seen["task"], task.id)
        self.assertEqual(task.status, TaskStatus.PARTIAL)
        self.assertIn("3 из 5", task.result)

    async def test_a_crashed_run_leaves_the_task_open(self):
        """Сорвавшийся прогон мог успеть записать часть — выдавать это за успех нельзя."""
        async def runner(price, task, content, guard):
            raise RuntimeError("1С недоступна")

        model = await self.build(runner)
        from tests.test_task_builder import workbook
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")
        price = model.prices[0]
        task = price.sorted_tasks[0]

        from src.model.commands import Command, CommandKind
        await model.apply(Command(kind=CommandKind.EXECUTE_TASK, actor="admin-1",
                                  price_id=price.id, task_id=task.id))

        self.assertEqual(task.status, TaskStatus.TODO)
        self.assertIn("проверьте в 1С", task.result)
        # ПРИЧИНА — В САМОМ РЕЗУЛЬТАТЕ. Прежний текст отсылал «в журнал», которого нет:
        # лог идёт в консоль процесса, и админ, читающий форму 1С, искал его в журнале
        # регистрации 1С и не находил.
        self.assertIn("1С недоступна", task.result)
        self.assertNotIn("в журнале", task.result)

    async def test_guard_refuses_after_the_lock_is_gone(self):
        """Сердцевина защиты: захват сняли посреди прогона — запись обязана отвалиться."""
        captured = {}

        async def runner(price, task, content, guard):
            guard()                                  # право ещё есть
            model._locks.pop(price.id, None)         # захват сняли
            try:
                guard()
                captured["refused"] = False
            except WriteRefused:
                captured["refused"] = True
            return TaskStatus.TODO, "проверка"

        model = await self.build(runner)
        from tests.test_task_builder import workbook
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")
        price = model.prices[0]
        task = price.sorted_tasks[0]

        from src.model.commands import Command, CommandKind
        await model.apply(Command(kind=CommandKind.EXECUTE_TASK, actor="admin-1",
                                  price_id=price.id, task_id=task.id))
        self.assertTrue(captured.get("refused"), "запись разрешена без захвата")

    async def test_guard_refuses_a_relocked_price(self):
        """Самый коварный случай: захват сняли и тут же взяли ЗАНОВО другим прогоном.

        Актор и живость совпадают — различает только поколение.
        """
        captured = {}

        async def runner(price, task, content, guard):
            import src.model.locks as lk
            old = model._locks[price.id]
            model._locks[price.id] = lk.acquire(old, price.id, "admin-1")
            try:
                guard()
                captured["refused"] = False
            except WriteRefused:
                captured["refused"] = True
            return TaskStatus.TODO, "проверка"

        model = await self.build(runner)
        from tests.test_task_builder import workbook
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")
        price = model.prices[0]
        task = price.sorted_tasks[0]

        from src.model.commands import Command, CommandKind
        await model.apply(Command(kind=CommandKind.EXECUTE_TASK, actor="admin-1",
                                  price_id=price.id, task_id=task.id))
        self.assertTrue(captured.get("refused"), "запись разрешена по чужому поколению")

    async def test_without_a_runner_it_stays_a_stub(self):
        model = await self.build(None)
        from tests.test_task_builder import workbook
        await model.submit(workbook(), "Прайс.xlsx", supplier_hint="Монарх")
        price = model.prices[0]
        task = price.sorted_tasks[0]

        from src.model.commands import Command, CommandKind
        await model.apply(Command(kind=CommandKind.EXECUTE_TASK, actor="admin-1",
                                  price_id=price.id, task_id=task.id))
        self.assertIn("ЗАГЛУШКА", task.result)


if __name__ == "__main__":
    unittest.main()
