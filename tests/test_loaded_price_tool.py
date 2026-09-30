"""Вопросы менеджера про ЗАГРУЖЕННЫЙ прайс (бой 30.09.2026).

Админ спросил «в какой колонке декоры на листе ЛАМИНАТ», а агент ответил, что прайса у
него нет, и попросил назвать поставщика, чтобы поискать письмо. Файл при этом лежал у нас
на диске: загруженный прайс живёт в МОДЕЛИ, а менеджерский агент видел только почту и сайт.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.agent.tools import PRICE_CHAT_ROWS, ToolExecutor


class Sheet:
    def __init__(self, name, rows):
        self.name = name
        self.rows = rows


class Price:
    def __init__(self, pid, filename="Прайс.xlsx", path="p.xlsx"):
        self.id = pid
        self.supplier_price = type("SP", (), {"filename": filename, "file_path": path})()


class Model:
    def __init__(self, prices):
        self.prices = list(prices)


SHEETS = [Sheet("ЛАМИНАТ", [["Артикул", "Декор", "Цена"],
                            ["LE-263", "Дуб Авола", "1150"],
                            ["LE-264", "Дуб Брен", "1180"]]),
          Sheet("ПЛИНТУС", [["Артикул", "Цена"]])]


class Base(unittest.IsolatedAsyncioTestCase):

    def executor(self, prices=(1,)):
        model = Model([Price(p) for p in prices]) if prices else Model([])
        return ToolExecutor(mail=None, norwik=None, model=model)

    async def ask(self, ex, **inp):
        with patch("src.storage.price_files.load", lambda *_a: b"xlsx"), \
             patch("src.price_tool.parser.parse_price_table", lambda *_a: list(SHEETS)):
            return await ex._read_loaded_price(inp)


class ReadTest(Base):

    async def test_sheet_rows_are_shown(self):
        text = await self.ask(self.executor(), sheet="ЛАМИНАТ")
        self.assertIn("Декор", text)
        self.assertIn("Дуб Авола", text)

    async def test_sheets_are_listed(self):
        """«Какие листы в прайсе» — ответ виден сразу, без второго вызова."""
        text = await self.ask(self.executor())
        self.assertIn("ЛАМИНАТ", text)
        self.assertIn("ПЛИНТУС", text)

    async def test_newest_price_is_taken_and_named(self):
        """«Этот прайс» по контексту не опознать — разговор идёт без истории. Поэтому
        берём самый свежий и обязаны сказать, какой именно."""
        ex = self.executor(prices=(1, 6, 4))
        text = await self.ask(ex)
        self.assertIn("Прайс №6", text)

    async def test_named_price_wins(self):
        text = await self.ask(self.executor(prices=(1, 6)), price_id=1)
        self.assertIn("Прайс №1", text)

    async def test_unknown_price_lists_what_there_is(self):
        text = await self.ask(self.executor(prices=(1, 6)), price_id=9)
        self.assertIn("№9 нет", text)
        self.assertIn("№1", text)
        self.assertIn("№6", text)

    async def test_unknown_sheet_says_so(self):
        text = await self.ask(self.executor(), sheet="ОБОИ")
        self.assertIn("Листа «ОБОИ»", text)
        self.assertIn("ЛАМИНАТ", text, "но листы перечислены — видно, что спрашивать")

    async def test_find_returns_only_matching_rows(self):
        text = await self.ask(self.executor(), sheet="ЛАМИНАТ", find="Брен")
        self.assertIn("Дуб Брен", text)
        self.assertNotIn("Дуб Авола", text)

    async def test_nothing_loaded(self):
        text = await self.ask(self.executor(prices=()))
        self.assertIn("нет ни одного загруженного", text)

    async def test_without_model_it_says_so_and_does_not_send_to_mail(self):
        ex = ToolExecutor(mail=None, norwik=None)
        self.assertIn("не настроена", await ex._read_loaded_price({}))

    async def test_missing_file_is_named(self):
        ex = self.executor()
        with patch("src.storage.price_files.load", lambda *_a: None):
            text = await ex._read_loaded_price({})
        self.assertIn("не найден на сервере", text)

    async def test_answer_is_capped(self):
        """Лист бывает на двенадцать тысяч строк: вопрос «в какой колонке» не должен
        стоить дороже всего разбора прайса."""
        big = [Sheet("ЛАМИНАТ", [[f"строка {i}", "x" * 200] for i in range(5000)])]
        ex = self.executor()
        with patch("src.storage.price_files.load", lambda *_a: b"xlsx"), \
             patch("src.price_tool.parser.parse_price_table", lambda *_a: big):
            text = await ex._read_loaded_price({})
        self.assertLessEqual(len(text), 12000)

    async def test_from_row_moves_the_window(self):
        rows = [[f"строка {i}"] for i in range(PRICE_CHAT_ROWS * 3)]
        big = [Sheet("ЛАМИНАТ", rows)]
        ex = self.executor()
        with patch("src.storage.price_files.load", lambda *_a: b"xlsx"), \
             patch("src.price_tool.parser.parse_price_table", lambda *_a: big):
            text = await ex._read_loaded_price({"from_row": PRICE_CHAT_ROWS * 2})
        self.assertIn(f"со строки {PRICE_CHAT_ROWS * 2}", text)


class ToolDefinitionTest(unittest.TestCase):

    def test_tool_is_offered_to_the_model(self):
        from src.agent.tools import TOOL_DEFINITIONS

        tool = next(t for t in TOOL_DEFINITIONS
                    if t.get("name") == "read_loaded_price")
        # Главное в описании: файл у нас, в почту ходить не надо — именно туда агент и
        # пошёл в бою.
        self.assertIn("почту", tool["description"].lower())

    def test_prompt_sends_such_questions_to_the_tool(self):
        from src.agent.prompts import SYSTEM_PROMPT

        self.assertIn("read_loaded_price", SYSTEM_PROMPT)
        self.assertIn("загруженный прайс", SYSTEM_PROMPT.lower())


if __name__ == "__main__":
    unittest.main()
