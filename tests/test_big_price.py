"""Большой мультибрендовый прайс: история диалога, поиск раздела, ход очереди.

Боевой случай 27.08.2026 (Артисан-Проект, лист на 12 870 строк): агент листал файл
подряд, каждая выгрузка оседала в переписке, за три хода история выросла до 560 000
символов — и модель перестала отвечать («Не удалось сформировать ответ»). Отдельно
прогон вставал на марке, у которой нечего менять: кнопки нет, а значит и обработчика,
который двинул бы очередь, тоже нет.
"""
import io as _io
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.agent.pricing_tools import PricingTools, clear_nomenclature_cache
from src.bot import pricing_handlers as ph
from src.bot.pricing_handlers import DUMP_STUB, _prune_file_dumps
from src.price_tool.parser import find_rows, parse_price_table
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec, item

TMS = [{"code": "T1", "name": "APE"}, {"code": "T2", "name": "Ceracasa"},
       {"code": "T3", "name": "Imola"}]


def dump(n: int) -> str:
    return "=== Лист: Price ===\n" + "строка данных прайса\t900\t1200\n" * n


def tool_result(text) -> dict:
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x",
                                         "content": text}]}


class PruneHistoryTest(unittest.TestCase):
    def test_old_dumps_replaced_newest_kept(self):
        messages = [
            {"role": "user", "content": "прайс"},
            tool_result(dump(400)),
            {"role": "assistant", "content": "разбираю"},
            tool_result(dump(400)),
        ]
        pruned = _prune_file_dumps(messages)
        self.assertEqual(pruned[1]["content"][0]["content"], DUMP_STUB)
        self.assertIn("строка данных", pruned[3]["content"][0]["content"])

    def test_history_shrinks_dramatically(self):
        messages = [tool_result(dump(400)) for _ in range(8)]
        before = len(str(messages))
        after = len(str(_prune_file_dumps(messages)))
        self.assertLess(after, before / 5)

    def test_short_results_untouched(self):
        """Обычные ответы инструментов не трогаем — режем только выгрузки листа."""
        messages = [tool_result("Маппинг сохранён."), tool_result("=== Лист: Price ===")]
        self.assertEqual(_prune_file_dumps(messages), messages)

    def test_blocks_with_images_are_pruned_too(self):
        blocks = [{"type": "text", "text": dump(400)},
                  {"type": "image", "source": {"data": "x" * 50000}}]
        messages = [tool_result(blocks), tool_result(dump(400))]
        pruned = _prune_file_dumps(messages)
        self.assertEqual(pruned[0]["content"][0]["content"], DUMP_STUB)

    def test_plain_text_messages_survive(self):
        messages = [{"role": "user", "content": "какой следующий бренд?"}]
        self.assertEqual(_prune_file_dumps(messages), messages)


class FindRowsTest(unittest.TestCase):
    def _sheet(self):
        wb = Workbook(); ws = wb.active; ws.title = "Price"
        ws.append(["Артикул", "Наименование", "Опт", "Розн"])
        for brand in ("APE", "Ceracasa", "Imola"):
            ws.append([f"=== {brand} ==="])
            for i in range(5):
                ws.append([f"{brand}-{i}", f"{brand} коллекция {i}", 900 + i, 1200 + i])
        buf = _io.BytesIO(); wb.save(buf)
        return parse_price_table(buf.getvalue(), "big.xlsx")[0]

    def test_reports_row_numbers_of_the_section(self):
        text = find_rows(self._sheet(), "Ceracasa")
        self.assertIn("найдено в 5 строках", text)
        self.assertIn("from_row", text)
        self.assertTrue(any(line.startswith("8\t") for line in text.splitlines()))

    def test_case_insensitive(self):
        self.assertIn("найдено в 5 строках", find_rows(self._sheet(), "ceRAcasa"))

    def test_missing_brand_says_so(self):
        text = find_rows(self._sheet(), "Kerama")
        self.assertIn("найдено в 0 строках", text)

    def test_empty_needle(self):
        self.assertIn("пустой запрос", find_rows(self._sheet(), "  "))


class FakeChat:
    def __init__(self):
        self.sent: list[str] = []

    async def answer(self, text, reply_markup=None):
        self.sent.append(text)
        return self

    async def edit_text(self, text, reply_markup=None):
        self.sent.append(text)

    async def delete(self):
        pass


class FakeOrchestrator:
    """Модель, которая на каждый ход закрывает очередную марку без изменений."""

    def __init__(self, tools_box):
        self.prompts: list[str] = []
        self._box = tools_box

    async def handle_turn(self, history, on_tool=None, system=None, extra_tools=None,
                          extra_executor=None, **kw):
        self.prompts.append(history[-1]["content"])
        self._box.append(extra_executor)
        await extra_executor.execute("propose_prices", {"groups": [{
            "tm_code": self._box[-1]._next_tm, "tm_name": "X",
            "collection_ref": "YO-C", "purchase": 949}]})
        return "марка без изменений", history


class AutoAdvanceTest(unittest.IsolatedAsyncioTestCase):
    """Марка без изменений не должна останавливать прогон."""

    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec([item("YO-1", 949, 1649, 1139)])
        await self.store.start_run(42, "Артисан", "Price.xls", TMS)
        ph._files[42] = ("Price.xls", b"x")

    async def asyncTearDown(self):
        self._dir.cleanup()
        ph._files.pop(42, None)

    async def test_queue_moves_without_admin(self):
        chat, box = FakeChat(), []
        codes = iter(["T1", "T2", "T3"])

        class Orc(FakeOrchestrator):
            async def handle_turn(self, history, **kw):
                self.prompts.append(history[-1]["content"])
                tools = kw["extra_executor"]
                await tools.execute("propose_prices", {"groups": [{
                    "tm_code": next(codes), "tm_name": "X",
                    "collection_ref": "YO-C", "purchase": 949}]})
                return "менять нечего", history

        await ph._run(chat, "прайс", Orc(box), self.onec, self.store, chat, user_id=42)

        text = "\n".join(chat.sent)
        self.assertIn("Перехожу к Ceracasa", text)
        self.assertIn("Перехожу к Imola", text)
        self.assertIn("Прайс «Price.xls» обработан полностью", text)
        self.assertIsNone(await self.store.get_run(42))     # прогон закрыт
        self.assertNotIn(42, ph._files)


if __name__ == "__main__":
    unittest.main()
