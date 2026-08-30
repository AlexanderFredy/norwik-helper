"""Очень крупный прайс: спросить админа до того, как тратиться (§6.10).

Прайс Артисана на 12 870 строк обошёлся примерно в 570 тыс. входных токенов за прогон.
Такое решение должен принимать админ, зная цифру, а не узнавать о ней из счёта.

«Пропустить» здесь НЕ то же, что «Отложить»: отложенное ждёт возврата и лежит в
/deferred, а пропущенный крупный прайс считается ОБРАБОТАННЫМ — админ обновит цены сам.
"""
import io as _io
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.agent.pricing_tools import (
    BIG_PRICE_ROWS, PricingTools, clear_nomenclature_cache,
)
from src.price_tool.parser import clear_parse_cache
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec, item


def book(rows: int) -> bytes:
    wb = Workbook(); ws = wb.active; ws.title = "Price"
    ws.append(["Артикул", "Наименование", "Опт", "Розн"])
    for i in range(rows):
        ws.append([f"A{i}", f"Плитка {i}", 900 + i % 500, 1400 + i % 700])
    buf = _io.BytesIO(); wb.save(buf); return buf.getvalue()


class BigPriceGateTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_nomenclature_cache(); clear_parse_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    def _tools(self, rows: int = 0, name="Price.xlsx", content: bytes | None = None):
        """content передаётся явно, когда нужен ИМЕННО тот же файл.

        `book()` каждый раз кладёт в xlsx свежие метаданные, поэтому два вызова с
        одинаковым числом строк дают разные байты — а «тот же прайс» опознаётся по хешу
        содержимого.
        """
        t = PricingTools(FakeOnec([item("YO-1", 949)]), self.store, user_id=42)
        t.set_file(name, content if content is not None else book(rows))
        return t

    async def test_small_price_passes_without_a_question(self):
        text = await self._tools(50).execute("read_price_file", {})
        self.assertNotIn("КРУПНЫЙ ПРАЙС", text)
        self.assertIn("Артикул", text)

    async def test_big_price_stops_and_asks(self):
        text = await self._tools(BIG_PRICE_ROWS + 10).execute("read_price_file", {})
        self.assertIn("КРУПНЫЙ ПРАЙС", text)
        self.assertIn("НЕ НАЧИНАЙ разбор", text)
        self.assertIn("обработать этот прайс или пропустить", text)
        self.assertIn("set_price_decision", text)
        # таблицы в ответе нет — платить за неё до решения админа незачем
        self.assertNotIn("Артикул", text)

    async def test_estimate_is_shown_and_scales(self):
        text = await self._tools(BIG_PRICE_ROWS + 10).execute("read_price_file", {})
        self.assertIn("входных токенов", text)
        self.assertIn("5 011 строк", text)

    async def test_next_months_price_asks_again(self):
        """Тот же формат, но другие строки — решение принималось не про этот файл."""
        tools = self._tools(BIG_PRICE_ROWS + 10)
        await tools.execute("read_price_file", {})
        await tools.execute("set_price_decision", {"decision": "manual"})
        nxt = self._tools(BIG_PRICE_ROWS + 300)       # прайс за следующий месяц
        self.assertIn("КРУПНЫЙ ПРАЙС", await nxt.execute("read_price_file", {}))

    async def test_process_decision_lets_it_through(self):
        tools = self._tools(BIG_PRICE_ROWS + 10)
        await tools.execute("read_price_file", {})
        answer = await tools.execute("set_price_decision", {"decision": "process"})
        self.assertIn("разбираем", answer)
        text = await tools.execute("read_price_file", {})
        self.assertNotIn("КРУПНЫЙ ПРАЙС", text)     # вопрос не повторяется
        self.assertIn("Артикул", text)

    async def test_manual_decision_marks_it_handled(self):
        tools = self._tools(BIG_PRICE_ROWS + 10)
        await tools.execute("read_price_file", {})
        answer = await tools.execute("set_price_decision", {
            "decision": "manual", "supplier": "Артисан-Проект",
            "price_date": "2026-08-26", "reason": "обновлю сам"})
        self.assertIn("вручную", answer)
        self.assertIn("НЕ идёт", answer)
        self.assertTrue(tools.handled_manually)

    async def test_manual_is_not_a_deferred_task(self):
        """Ключевое отличие от «Отложить»: возвращаться к нему не нужно."""
        tools = self._tools(BIG_PRICE_ROWS + 10)
        await tools.execute("read_price_file", {})
        await tools.execute("set_price_decision", {"decision": "manual"})
        self.assertEqual(await self.store.list_deferred(42), [])
        self.assertEqual(len(await self.store.list_manual()), 1)

    async def test_manual_remembered_for_the_same_file(self):
        content = book(BIG_PRICE_ROWS + 10)
        tools = self._tools(content=content)
        await tools.execute("read_price_file", {})
        await tools.execute("set_price_decision", {"decision": "manual"})

        again = self._tools(content=content)          # ровно тот же файл
        text = await again.execute("read_price_file", {})
        self.assertIn("ВРУЧНУЮ", text)
        self.assertIn("считается обработанным", text)
        self.assertNotIn("Артикул", text)

    async def test_another_file_asks_again(self):
        tools = self._tools(BIG_PRICE_ROWS + 10)
        await tools.execute("read_price_file", {})
        await tools.execute("set_price_decision", {"decision": "manual"})
        other = self._tools(BIG_PRICE_ROWS + 500)
        self.assertIn("КРУПНЫЙ ПРАЙС", await other.execute("read_price_file", {}))

    async def test_decision_needs_a_file(self):
        tools = PricingTools(FakeOnec([]), self.store, user_id=42)
        self.assertIn("Не удалось опознать файл",
                      await tools.execute("set_price_decision", {"decision": "manual"}))

    async def test_unknown_decision_falls_back_to_process(self):
        tools = self._tools(BIG_PRICE_ROWS + 10)
        await tools.execute("set_price_decision", {"decision": "ерунда"})
        self.assertFalse(tools.handled_manually)
        self.assertEqual(await self.store.list_manual(), [])


if __name__ == "__main__":
    unittest.main()
