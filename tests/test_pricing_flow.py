"""Сценарий режима цен без Telegram и без сети: предложение → сохранение → payload.

Проверяется ключевое свойство гейта (§10): в 1С уходит РОВНО тот payload, который был
сохранён при показе предложения, и только один раз.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from src.agent.pricing_tools import PricingTools, clear_nomenclature_cache
from src.onec.client import NomItem, Nomenclature, Price
from src.storage.pricing import PricingStore


def item(ref, purchase, rrc=None, retail=None, coll="YO-C"):
    def p(v, dt="2026-08-01"):
        return Price(value=float(v), date=dt) if v is not None else None
    return NomItem(ref=ref, id="1", name=f"Товар {ref}", article="", unit="м2", size="",
                   product_type="Ламинат", collection="Vintage", parent="Vintage",
                   collection_ref=coll, alt_units={}, purchase=p(purchase),
                   retail=p(retail), rrc=p(rrc))


class FakeOnec:
    def __init__(self, items):
        self._items = items
        self.written: list[list[dict]] = []
        self.reads = 0
        self.errors: list[dict] = []      # позиции, которые 1С не отдала
        self.tms: list[tuple[str, str]] = []   # (имя, код) для selling_tm

    def selling_tm(self):
        from src.onec.client import TradeMark
        return [TradeMark(name=n, code=c) for n, c in self.tms]

    def by_tm_all(self, tm_code, **kw):
        self.reads += 1
        return Nomenclature(tm=tm_code, total=len(self._items), items=self._items,
                            errors=self.errors)

    def set_prices(self, items):
        self.written.append(items)
        return {"date": "2026-08-11", "updated": len(items), "results": [], "errors": []}


class PricingFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_nomenclature_cache()      # кэш номенклатуры общий на процесс
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec([item("YO-1", 949, 1649, 1139), item("YO-2", 949, 1649, 1139)])
        self.tools = PricingTools(self.onec, self.store, user_id=42)

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _propose(self, purchase=999, rrc=1649):
        return await self.tools.execute("propose_prices", {
            "supplier": "LINDERWOOD",
            "groups": [{"tm_code": "000000298", "tm_name": "Peli",
                        "collection_ref": "YO-C", "purchase": purchase, "rrc": rrc}],
        })

    async def test_proposal_saved_with_payload(self):
        text = await self._propose()
        self.assertIn("Обновляем цены от LINDERWOOD", text)
        pending = await self.store.get_pending(42)
        self.assertIsNotNone(pending)
        # закупка выросла на 5.3% → пишем; розница 999×1.2 = 1199
        self.assertEqual(pending.payload[0]["prices"]["purchase"], 999.0)
        self.assertEqual(pending.payload[0]["prices"]["retail"], 1199.0)
        self.assertIn("collection_ref", pending.payload[0])       # форма «а»

    async def test_kopeck_change_produces_no_proposal(self):
        text = await self._propose(purchase=950, rrc=1649)        # +0.1% → ниже порога
        self.assertIsNone(await self.store.get_pending(42))
        self.assertIn("Записывать нечего", text)

    async def test_apply_uses_saved_payload_once(self):
        await self._propose()
        pending = await self.store.get_pending(42)
        taken = await self.store.take_pending(42, pending.proposal_id)
        self.assertEqual(taken.payload, pending.payload)
        self.onec.set_prices(taken.payload)
        await self.store.mark_applied(taken.proposal_id)
        self.assertEqual(self.onec.written[0], pending.payload)
        # повторное нажатие кнопки не должно ничего записать
        self.assertIsNone(await self.store.take_pending(42, pending.proposal_id))

    async def test_failed_write_returns_proposal_to_queue(self):
        """Обрыв связи с 1С не должен стоить админу повторного разбора прайса."""
        await self._propose()
        pending = await self.store.get_pending(42)
        taken = await self.store.take_pending(42, pending.proposal_id)
        self.assertIsNone(await self.store.get_pending(42))      # взято в работу
        await self.store.release(taken.proposal_id)              # запись упала
        again = await self.store.get_pending(42)
        self.assertIsNotNone(again)
        self.assertEqual(again.proposal_id, pending.proposal_id)
        self.assertEqual(again.payload, pending.payload)

    async def test_release_does_not_resurrect_applied(self):
        await self._propose()
        pending = await self.store.get_pending(42)
        await self.store.take_pending(42, pending.proposal_id)
        await self.store.mark_applied(pending.proposal_id)
        await self.store.release(pending.proposal_id)            # запоздалый откат
        self.assertIsNone(await self.store.get_pending(42))

    def _xlsx(self, title: str) -> bytes:
        import io as _io
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active; ws.title = "Прайс"
        ws.append([title])
        ws.append(["Артикул", "Наименование", "самовывоз", "с доставкой", "РРЦ"])
        ws.append(["VN-511", "Ван Браун", 949, 999, 1649])
        buf = _io.BytesIO(); wb.save(buf); return buf.getvalue()

    async def test_mapping_saved_and_applied_next_time(self):
        """Ответ админа про колонки должен переживать смену прайса того же формата."""
        self.tools.set_file("price-july.xlsx", self._xlsx("Прайс с 20.07.2026"))
        first = await self.tools.execute("read_price_file", {})
        self.assertIn("встречается впервые", first)

        await self.tools.execute("save_price_mapping", {
            "supplier": "LINDERWOOD", "purchase_column": "самовывоз",
            "rrc_column": "РРЦ", "basis": "base_unit",
            "note": "админ сказал брать минимальные цены"})

        # следующий месяц: другой файл, другие данные, тот же формат
        later = PricingTools(self.onec, self.store, user_id=42)
        later.set_file("price-august.xlsx", self._xlsx("Прайс с 25.08.2026"))
        text = await later.execute("read_price_file", {})
        self.assertIn("ЗАПОМНЕННЫЙ МАППИНГ", text)
        self.assertIn("самовывоз", text)
        self.assertIn("МОЛЧА", text)
        # запомненный лист не должен отменять разбор остальных листов
        self.assertIn("ОСТАЛЬНЫЕ листы", text)

    async def test_mapping_is_overwritten_when_admin_changes_mind(self):
        self.tools.set_file("p.xlsx", self._xlsx("Прайс"))
        await self.tools.execute("save_price_mapping", {"purchase_column": "самовывоз"})
        await self.tools.execute("save_price_mapping", {"purchase_column": "с доставкой"})
        text = await self.tools.execute("read_price_file", {})
        self.assertIn("с доставкой", text)
        self.assertNotIn("«самовывоз»", text)

    async def test_unchanged_collections_are_collapsed(self):
        """Коллекции без изменений не должны прятать собой то, что меняется."""
        self.onec._items = [item("YO-1", 949, coll="YO-A"), item("YO-2", 500, coll="YO-B")]
        text = await self.tools.execute("propose_prices", {
            "supplier": "X",
            "groups": [
                {"tm_code": "T", "tm_name": "TM", "collection_ref": "YO-A", "purchase": 949},
                {"tm_code": "T", "tm_name": "TM", "collection_ref": "YO-B", "purchase": 600},
            ]})
        self.assertIn("без изменений (1)", text)
        self.assertIn("закупка 500 → 600", text)

    async def test_new_proposal_supersedes_old(self):
        await self._propose(purchase=999)
        first = await self.store.get_pending(42)
        await self._propose(purchase=1200)
        second = await self.store.get_pending(42)
        self.assertNotEqual(first.proposal_id, second.proposal_id)
        self.assertIsNone(await self.store.take_pending(42, first.proposal_id))

    async def test_cancel_clears_pending(self):
        await self._propose()
        pending = await self.store.get_pending(42)
        self.assertTrue(await self.store.reject(42, pending.proposal_id))
        self.assertIsNone(await self.store.get_pending(42))

    async def test_nomenclature_survives_between_turns(self):
        """Каждый ответ админа создаёт новый PricingTools — 1С не должна перекачиваться.

        Из-за этого мультибрендовый прайс «зависал»: ответ «плинтус не трогай» заново
        тянул номенклатуру всех ТМ прайса.
        """
        await self._propose()
        self.assertEqual(self.onec.reads, 1)

        next_turn = PricingTools(self.onec, self.store, user_id=42)   # следующий ход
        await next_turn.execute("get_1c_nomenclature", {"tm_code": "000000298"})
        self.assertEqual(self.onec.reads, 1)                          # взято из кэша

        clear_nomenclature_cache()                                    # новый прайс
        await next_turn.execute("get_1c_nomenclature", {"tm_code": "000000298"})
        self.assertEqual(self.onec.reads, 2)

    async def test_positions_1c_could_not_return_reach_the_admin(self):
        """1С отдаёт часть позиций с ошибкой — предложение обязано это показать.

        Иначе непроверенные по прайсу товары просто исчезают из сопоставления.
        """
        self.onec.errors = [{"ref": "YO-00032200", "code": "item_failed",
                             "message": "Значение не является значением объектного типа"}]
        text = await self._propose()
        self.assertIn("1С не отдала 1 поз.", text)
        self.assertIn("YO-00032200", text)
        self.assertIn("НЕ проверены по прайсу", text)

    async def test_nomenclature_tool_reports_missing_positions(self):
        self.onec.errors = [{"ref": "YO-1", "code": "item_failed", "message": "сбой"}]
        out = await self.tools.execute("get_1c_nomenclature", {"tm_code": "000000298"})
        self.assertIn("not_returned_by_1c", out)
        self.assertIn("YO-1", out)

    async def test_clean_response_has_no_warning(self):
        text = await self._propose()
        self.assertNotIn("не отдала", text)

    async def test_dialog_history_roundtrip(self):
        await self.store.save_messages(42, [{"role": "user", "content": "привет"}])
        self.assertEqual(await self.store.load_messages(42),
                         [{"role": "user", "content": "привет"}])
        await self.store.reset(42)
        self.assertEqual(await self.store.load_messages(42), [])


if __name__ == "__main__":
    unittest.main()
