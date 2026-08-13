"""Категории товаров, которые анализируем (§6.8): сопоставление, хранение, команды."""
import tempfile
import unittest
from pathlib import Path

from src.bot import pricing_handlers as ph
from src.price_tool.scope import describe, in_scope, matches, normalize, split
from src.storage.pricing import PricingStore


class NormalizeTest(unittest.TestCase):
    def test_case_punctuation_and_yo(self):
        self.assertEqual(normalize("  Керамическая   плитка, "), "керамическая плитка")
        self.assertEqual(normalize("Клён"), "клен")

    def test_empty(self):
        self.assertEqual(normalize(None), "")
        self.assertEqual(normalize("   "), "")


class MatchesTest(unittest.TestCase):
    def test_short_admin_word_covers_long_1c_name(self):
        """Админ пишет «плитка», в 1С «Керамическая плитка»."""
        self.assertTrue(matches("плитка", "Керамическая плитка"))

    def test_long_admin_word_covers_short(self):
        self.assertTrue(matches("Двери межкомнатные", "двери"))

    def test_exact(self):
        self.assertTrue(matches("Ламинат", "ламинат"))

    def test_unrelated(self):
        self.assertFalse(matches("ламинат", "Обои"))
        self.assertFalse(matches("ламинат", "Плинтус"))

    def test_empty_never_matches(self):
        """Иначе пустая строка совпала бы со всем подряд."""
        self.assertFalse(matches("", "Ламинат"))
        self.assertFalse(matches("ламинат", None))


class InScopeTest(unittest.TestCase):
    def test_empty_scope_allows_everything(self):
        """Пустой список — «ограничений нет», а не «ничего не анализируем»."""
        self.assertTrue(in_scope([], "Ламинат"))
        self.assertTrue(in_scope([], "Плинтус"))

    def test_filters(self):
        scope = ["ламинат", "плитка"]
        self.assertTrue(in_scope(scope, "Виниловый ламинат"))
        self.assertTrue(in_scope(scope, "Керамическая плитка"))
        self.assertFalse(in_scope(scope, "Плинтус"))
        self.assertFalse(in_scope(scope, "Обои"))

    def test_split_keeps_order_and_dedups(self):
        watched, skipped = split(["ламинат"],
                                 ["Ламинат", "Плинтус", "Ламинат", None, "Подложка"])
        self.assertEqual(watched, ["Ламинат"])
        self.assertEqual(skipped, ["Плинтус", "Подложка"])


class DescribeTest(unittest.TestCase):
    def test_empty_says_no_limits(self):
        self.assertIn("Ограничений", describe([]))

    def test_lists_categories(self):
        text = describe(["ламинат", "обои"])
        self.assertIn("ламинат, обои", text)
        self.assertIn("НЕ разбирай", text)


class ScopeStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_add_list_remove(self):
        self.assertEqual(await self.store.add_scope(["Ламинат", "Обои"]),
                         ["Ламинат", "Обои"])
        self.assertEqual([c["category"] for c in await self.store.list_scope()],
                         ["Ламинат", "Обои"])
        self.assertTrue(await self.store.remove_scope("ламинат"))   # регистр не важен
        self.assertEqual([c["category"] for c in await self.store.list_scope()], ["Обои"])

    async def test_duplicates_ignored(self):
        await self.store.add_scope(["Ламинат"])
        self.assertEqual(await self.store.add_scope(["  ламинат  ", "Обои"]), ["Обои"])

    async def test_remove_unknown(self):
        self.assertFalse(await self.store.remove_scope("плинтус"))

    async def test_blank_ignored(self):
        self.assertEqual(await self.store.add_scope(["", "   ", ","]), [])


class FakeMessage:
    def __init__(self, user_id=42):
        self.from_user = type("U", (), {"id": user_id})()
        self.sent: list[str] = []

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.sent.append(text)
        return self

    @property
    def text(self) -> str:
        return "\n".join(self.sent)


class Args:
    def __init__(self, args): self.args = args


class ScopeCommandsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_empty_explains_default(self):
        msg = FakeMessage()
        await ph.cmd_categories(msg, self.store, is_admin=True)
        self.assertIn("Ограничений по категориям нет", msg.text)
        self.assertIn("/category_add", msg.text)

    async def test_add_several_at_once(self):
        msg = FakeMessage()
        await ph.cmd_category_add(msg, Args("ламинат, керамическая плитка,обои"),
                                  self.store, is_admin=True)
        self.assertIn("Добавил: ламинат, керамическая плитка, обои", msg.text)
        self.assertEqual(len(await self.store.list_scope()), 3)

    async def test_first_add_warns_about_semantics_change(self):
        """Переход «пусто → список» меняет смысл с «всё» на «только это»."""
        msg = FakeMessage()
        await ph.cmd_category_add(msg, Args("ламинат"), self.store, is_admin=True)
        self.assertIn("ТОЛЬКО перечисленное", msg.text)

    async def test_later_add_does_not_warn(self):
        await self.store.add_scope(["ламинат"])
        msg = FakeMessage()
        await ph.cmd_category_add(msg, Args("обои"), self.store, is_admin=True)
        self.assertNotIn("ТОЛЬКО перечисленное", msg.text)

    async def test_removing_last_warns(self):
        await self.store.add_scope(["ламинат"])
        msg = FakeMessage()
        await ph.cmd_category_remove(msg, Args("ламинат"), self.store, is_admin=True)
        self.assertIn("анализируем всё", msg.text)

    async def test_list_shows_categories(self):
        await self.store.add_scope(["Ламинат", "Обои"])
        msg = FakeMessage()
        await ph.cmd_categories(msg, self.store, is_admin=True)
        self.assertIn("1. Ламинат", msg.text)
        self.assertIn("2. Обои", msg.text)

    async def test_manager_denied(self):
        for handler, args in ((ph.cmd_categories, None),
                              (ph.cmd_category_add, Args("ламинат")),
                              (ph.cmd_category_remove, Args("ламинат"))):
            msg = FakeMessage()
            if args is None:
                await handler(msg, self.store, is_admin=False)
            else:
                await handler(msg, args, self.store, is_admin=False)
            self.assertIn("только администратору", msg.text)

    async def test_add_without_args(self):
        msg = FakeMessage()
        await ph.cmd_category_add(msg, Args(""), self.store, is_admin=True)
        self.assertIn("через запятую", msg.text)


if __name__ == "__main__":
    unittest.main()
