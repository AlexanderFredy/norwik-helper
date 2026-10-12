"""Комментарии админа по марке у формата прайса (решение админа 12.10.2026).

Указание вида «CL/CR — левая и правая доска, разные товары» пишется один раз в форме
формата и действует на каждом прайсе того же поставщика: агент читает его и при сборке
задач, и при выполнении. Привязано к МАРКЕ 1С; у формата без брендов для этого заводится
строка «бренд прайса».
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.model.commands import Command, CommandKind
from src.storage.suppliers import PRICE_BRAND, SupplierStore


class StoreTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = SupplierStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        supplier = await self.store.add_supplier("Керамика")
        self.sig = await self.store.add_signature(supplier.id, "hash-1")
        await self.store.remember_marks("hash-1", [("ABK", 22, "000000265", "ABK"),
                                                   ("ABK Sensi", 5, "000000265", "ABK"),
                                                   ("Dogma", 3, "", "")])

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def save(self, rows):
        return await self.store.set_marks_by_signature("hash-1", rows)

    async def test_note_is_kept_by_mark_and_shared_by_its_brands(self):
        await self.save([{"brand": "ABK", "tm_code": "000000265", "tm_name": "ABK",
                          "note": "CL/CR — разные товары"}])
        rows = {m.brand: m for m in await self.store.marks_for("hash-1")}
        self.assertEqual(rows["ABK"].note, "CL/CR — разные товары")
        self.assertEqual(rows["ABK Sensi"].note, "CL/CR — разные товары", "та же марка")
        self.assertEqual(rows["Dogma"].note, "", "без марки — не к чему привязать")
        self.assertEqual(await self.store.notes_for("hash-1"),
                         {"000000265": "CL/CR — разные товары"})

    async def test_empty_note_erases(self):
        await self.save([{"brand": "ABK", "tm_code": "000000265", "note": "текст"}])
        await self.save([{"brand": "ABK", "tm_code": "000000265", "note": ""}])
        self.assertEqual(await self.store.notes_for("hash-1"), {})

    async def test_old_form_without_the_key_does_not_erase(self):
        """Команда прежней формы о комментарии не знает — стирать его она не вправе."""
        await self.save([{"brand": "ABK", "tm_code": "000000265", "note": "текст"}])
        await self.save([{"brand": "ABK", "tm_code": "000000265", "parse": True}])
        self.assertEqual(await self.store.notes_for("hash-1"), {"000000265": "текст"})

    async def test_note_without_a_mark_is_not_stored(self):
        await self.save([{"brand": "Dogma", "tm_code": "", "note": "текст"}])
        self.assertEqual(await self.store.notes_for("hash-1"), {})

    async def test_note_follows_a_rehash(self):
        await self.save([{"brand": "ABK", "tm_code": "000000265", "note": "текст"}])
        await self.store.rehash_signature(self.sig.id, "hash-new")
        self.assertEqual(await self.store.notes_for("hash-new"), {"000000265": "текст"})


class PriceBrandTest(unittest.IsolatedAsyncioTestCase):
    """Формат без брендов: одна строка «бренд прайса» под марку и комментарий."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = SupplierStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        supplier = await self.store.add_supplier("Эггер")
        await self.store.add_signature(supplier.id, "egger")

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def scanned(self):
        from src.price_tool.brand_rows import BY_NONE, RULE_VERSION
        await self.store.set_signature_brand_col("egger", self.store.NO_BRAND_COLUMN,
                                                 BY_NONE, RULE_VERSION)

    async def test_not_scanned_gets_nothing(self):
        """Не смотревшему формату бренды ещё могут найтись — заглушка тут соврала бы."""
        self.assertEqual(await self.store.ensure_price_brands(), 0)
        self.assertEqual(await self.store.marks_for("egger"), [])

    async def test_scanned_without_brands_gets_one_row_once(self):
        await self.scanned()
        self.assertEqual(await self.store.ensure_price_brands(), 1)
        self.assertEqual(await self.store.ensure_price_brands(), 0, "идемпотентно")
        rows = await self.store.marks_for("egger")
        self.assertEqual([m.brand for m in rows], [PRICE_BRAND])
        self.assertTrue(rows[0].is_price_brand)

    async def test_admin_sets_mark_and_note_on_it(self):
        await self.scanned()
        await self.store.ensure_price_brands()
        await self.store.set_marks_by_signature("egger", [
            {"brand": PRICE_BRAND, "tm_code": "000000006", "tm_name": "Egger / Эггер",
             "note": "CL и CR — левая и правая доска, это разные товары"}])
        self.assertEqual(await self.store.notes_for("egger"),
                         {"000000006": "CL и CR — левая и правая доска, это разные товары"})

    async def test_real_brands_replace_it_but_the_note_stays_with_the_mark(self):
        await self.scanned()
        await self.store.ensure_price_brands()
        await self.store.set_marks_by_signature("egger", [
            {"brand": PRICE_BRAND, "tm_code": "000000006", "note": "текст"}])
        await self.store.remember_marks("egger", [("EGGER", 40, "000000006", "Egger")])
        rows = await self.store.marks_for("egger")
        self.assertEqual([m.brand for m in rows], ["EGGER"])
        self.assertEqual(rows[0].note, "текст")

    async def test_intake_without_brands_adds_it(self):
        from unittest.mock import patch
        from src.model import brand_intake

        with patch.object(brand_intake, "collect", lambda *a, **k: (None, [], "нет")):
            await brand_intake.remember(self.store, "egger", b"", "x.xls")
        self.assertEqual([m.brand for m in await self.store.marks_for("egger")],
                         [PRICE_BRAND])


class ServiceTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        from src.model.service import PriceListService
        from src.storage import price_files
        from src.storage.model_store import ModelStore

        self._dir = tempfile.TemporaryDirectory()
        db = Path(self._dir.name) / "t.db"
        self.model_store = ModelStore(db)
        await self.model_store.init()
        self.store = SupplierStore(db)
        await self.store.init()
        supplier = await self.store.add_supplier("Эггер")
        await self.store.add_signature(supplier.id, "egger")
        await self.store.ensure_price_brand("egger")
        self.model = PriceListService(
            self.model_store, self.store,
            save_file=lambda content, name: price_files.save(db, name, content))
        await self.model.load()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_price_brand_alone_is_not_called_unmarked_brands(self):
        """«Бренды не отмечены, разбирать нечего» про формат без брендов — неправда."""
        said = await self.model._apply_marks("egger", [
            {"brand": PRICE_BRAND, "parse": False, "tm_code": "000000006", "note": "текст"}])
        self.assertNotIn("разбирать нечего", said)
        self.assertIn("ограничивают листы", said)
        self.assertIn("Комментариев по маркам: 1", said)

    async def test_command_from_the_form_stores_the_note(self):
        await self.model.apply(Command(
            kind=CommandKind.SET_SIGNATURE_MARKS, source="1c", actor="1c:Саша",
            payload={"signature": "egger", "marks": [
                {"brand": PRICE_BRAND, "tm_code": "000000006", "note": "текст"}]}))
        self.assertEqual(await self.store.notes_for("egger"), {"000000006": "текст"})


class MirrorTest(unittest.IsolatedAsyncioTestCase):

    async def test_snapshot_carries_the_note(self):
        from src.onec.model_provider import OnecProvider

        with tempfile.TemporaryDirectory() as tmp:
            store = SupplierStore(Path(tmp) / "t.db")
            await store.init()
            supplier = await store.add_supplier("Эггер")
            await store.add_signature(supplier.id, "egger")
            await store.ensure_price_brand("egger")
            await store.set_marks_by_signature("egger", [
                {"brand": PRICE_BRAND, "tm_code": "000000006", "note": "текст"}])

            service = SimpleNamespace(prices=[], lock_of=lambda _id: None)
            rows = await OnecProvider(onec=None, service=service,
                                      suppliers=store).marks_snapshot()
            self.assertEqual([(r["brand"], r["tm_code"], r["note"]) for r in rows],
                             [(PRICE_BRAND, "000000006", "текст")])


class PromptTest(unittest.TestCase):

    def test_builder_gets_the_notes(self):
        from src.model.task_builder import admin_notes_block

        block = admin_notes_block([("000000006", "Egger / Эггер",
                                    "CL и CR — разные товары")])
        self.assertIn("УКАЗАНИЯ АДМИНА ПО МАРКАМ", block)
        self.assertIn("— Egger / Эггер: CL и CR — разные товары", block)
        self.assertIn("не проси уточнить у поставщика", block)
        self.assertEqual(admin_notes_block([]), "")
        self.assertEqual(admin_notes_block([("1", "X", "  ")]), "")

    def test_executor_gets_the_note_above_the_description(self):
        from src.model.enums import TaskKind
        from src.model.executor import task_brief

        address = SimpleNamespace(tm=SimpleNamespace(code="000000006"),
                                  subject=SimpleNamespace(article=""),
                                  label=lambda: "Egger / 8/33 Широкий ёлочка")
        task = SimpleNamespace(id=1038, kind=TaskKind.ADD_NEW, address=address,
                               subject=SimpleNamespace(value="коллекция"),
                               description="СНАЧАЛА УТОЧНИТЬ", result="")
        price = SimpleNamespace(id=11, supplier_price=SimpleNamespace(filename="ЭГГЕР.xls"))
        brief = task_brief(price, task, admin_note="CL и CR — разные товары")
        self.assertIn("УКАЗАНИЕ АДМИНА ПО ЭТОЙ МАРКЕ", brief)
        self.assertLess(brief.index("CL и CR"), brief.index("СНАЧАЛА УТОЧНИТЬ"))
        self.assertNotIn("УКАЗАНИЕ АДМИНА", task_brief(price, task))


if __name__ == "__main__":
    unittest.main()
