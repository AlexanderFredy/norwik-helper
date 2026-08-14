"""Отчёт после записи цен: длина, сжатие и гарантия доставки.

Регрессия из боевого прогона 14.08.2026: 62 строки запроса дали отчёт на 10 065 символов,
`edit_text` его отверг (лимит Telegram 4096), исключение вышло из обработчика — цены в 1С
записались, а админ не увидел ничего.

Главный инвариант: после записи цен админ ОБЯЗАН получить ответ. Молчание неотличимо от
«ничего не произошло», хотя цены уже изменены.
"""
import unittest

from src.bot import pricing_handlers as ph
from src.bot.pricing_handlers import _chunks, _deliver, _format_result
from tests.test_broadcast import DIGEST, FakeBot, FakeUserStore


def per_item(n: int) -> dict:
    """Ответ 1С формы «б»: по одному результату на товар."""
    return {"date": "2026-08-14", "updated": n * 3, "results": [
        {"ref": f"YO-{i:05}", "name": f"Ламинат Egger Large 4V Дуб Ноксвилл {i}",
         "written": {k: {"old": 800, "new": 930} for k in ("purchase", "rrc", "retail")}}
        for i in range(n)], "errors": []}


class FakeMessage:
    def __init__(self, fail_edit=False):
        self.fail_edit = fail_edit
        self.edited: list[str] = []
        self.answered: list[str] = []

    async def edit_text(self, text, reply_markup=None):
        if self.fail_edit:
            raise RuntimeError("Bad Request: message is too long")
        self.edited.append(text)

    async def answer(self, text, reply_markup=None):
        self.answered.append(text)
        return self


class FormatResultTest(unittest.TestCase):
    def test_per_item_rows_are_collapsed(self):
        """43 товара с одинаковым переходом — три строки, а не 129."""
        text = _format_result(per_item(43))
        self.assertIn("по отдельным товарам (129)", text)
        self.assertIn("закуп: 800 → 930 — 43 поз.", text)
        self.assertLess(len(text.splitlines()), 12)

    def test_single_item_keeps_its_name(self):
        text = _format_result(per_item(1))
        self.assertIn("Дуб Ноксвилл 0", text)
        self.assertNotIn("1 поз.", text)

    def test_real_scale_report_fits_one_message(self):
        """Боевой случай: 19 коллекций + 43 товара укладываются в лимит."""
        result = per_item(43)
        result["results"] += [
            {"tm_name": "Egger", "collection": f"Коллекция {i}", "count": 7,
             "changes": [{"price_type": k, "old": 800, "new": 930, "count": 7}
                         for k in ("purchase", "rrc", "retail")]} for i in range(19)]
        self.assertLess(len(_format_result(result)), 4096)

    def test_price_types_are_russian(self):
        self.assertIn("закуп", _format_result(per_item(1)))
        self.assertNotIn("purchase", _format_result(per_item(1)))

    def test_errors_still_listed_and_capped(self):
        result = per_item(1)
        result["errors"] = [{"code": "product_not_found", "message": f"нет {i}"}
                            for i in range(25)]
        text = _format_result(result)
        self.assertIn("Ошибки (25)", text)
        self.assertIn("и ещё 5", text)

    def test_unchanged_reported(self):
        result = per_item(1); result["unchanged"] = 12
        self.assertIn("Без изменений: 12 поз.", _format_result(result))


class ChunksTest(unittest.TestCase):
    def test_splits_over_limit(self):
        parts = _chunks("x" * 9000)
        self.assertEqual([len(p) for p in parts], [4096, 4096, 808])

    def test_empty_text_still_yields_one_part(self):
        self.assertEqual(_chunks(""), [""])


class DeliverTest(unittest.IsolatedAsyncioTestCase):
    async def test_short_report_edits_status_in_place(self):
        progress, chat = FakeMessage(), FakeMessage()
        await _deliver(progress, chat, "коротко")
        self.assertEqual(progress.edited, ["коротко"])
        self.assertEqual(chat.answered, [])

    async def test_long_report_is_split(self):
        progress, chat = FakeMessage(), FakeMessage()
        await _deliver(progress, chat, "y" * 9000)
        self.assertEqual(len(progress.edited[0]), 4096)
        self.assertEqual([len(p) for p in chat.answered], [4096, 808])

    async def test_failed_edit_falls_back_to_new_message(self):
        """Именно это и произошло на бою — отчёт не должен пропасть."""
        progress, chat = FakeMessage(fail_edit=True), FakeMessage()
        await _deliver(progress, chat, "важный отчёт")
        self.assertEqual(chat.answered, ["важный отчёт"])

    async def test_total_delivery_failure_does_not_raise(self):
        """Даже когда доставить некуда, обработчик не должен падать: цены уже записаны."""
        class Dead(FakeMessage):
            async def answer(self, text, reply_markup=None):
                raise RuntimeError("chat not found")
        await _deliver(FakeMessage(fail_edit=True), Dead(), "отчёт")   # не бросает


class NotifyChunkTest(unittest.IsolatedAsyncioTestCase):
    async def test_long_broadcast_is_split_per_manager(self):
        bot = FakeBot()
        big = {"groups": [{
            "tm_code": "T", "tm_name": "Peli " + "о" * 200, "collection": "К" * 200,
            "collection_ref": f"YO-{i}",
            "items": [{"ref": f"r{i}", "prices": {"purchase": [949, 999]}}],
        } for i in range(40)]}
        sent = await ph._notify_managers(bot, FakeUserStore([7]), big,
                                         {"errors": []}, admin_id=42)
        self.assertEqual(sent, 1)
        self.assertGreater(len(bot.sent), 1)                  # ушло несколькими частями
        self.assertTrue(all(len(t) <= 4096 for _, t in bot.sent))

    async def test_short_broadcast_stays_one_message(self):
        bot = FakeBot()
        await ph._notify_managers(bot, FakeUserStore([7]), DIGEST,
                                  {"errors": []}, admin_id=42)
        self.assertEqual(len(bot.sent), 1)


if __name__ == "__main__":
    unittest.main()
