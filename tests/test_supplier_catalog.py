"""Справочники поставщиков, сигнатур и файлов (§2 spec/agent-workflow-model.md).

До справочника поставщик был свободной строкой, которую называла LLM: вывод нигде не
закреплялся, и на следующем прайсе опознание начиналось заново. Здесь проверяется то, на чём
такой справочник ломается, — нормализация имён, сигнатура у двух владельцев и слияние дублей.
"""
import tempfile
import unittest
from pathlib import Path

from src.price_tool import catalog_view as view
from src.storage.suppliers import SupplierStore


class Base(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = SupplierStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()


class SupplierTest(Base):

    async def test_added_and_found(self):
        made = await self.store.add_supplier("Монарх Логистик")
        found = await self.store.find_supplier("Монарх Логистик")
        self.assertEqual(found.id, made.id)

    async def test_same_name_is_not_a_second_supplier(self):
        """Приём прайса зовёт add на каждом файле — «уже есть» нормальный исход."""
        first = await self.store.add_supplier("Монарх")
        again = await self.store.add_supplier("Монарх")
        self.assertEqual(first.id, again.id)
        self.assertEqual(len(await self.store.list_suppliers()), 1)

    async def test_case_and_punctuation_do_not_create_duplicates(self):
        a = await self.store.add_supplier("Монарх-Логистик")
        b = await self.store.add_supplier("  монарх логистик  ")
        self.assertEqual(a.id, b.id)

    async def test_display_name_keeps_the_original_form(self):
        made = await self.store.add_supplier("MOST Flooring")
        self.assertEqual(made.name, "MOST Flooring")

    async def test_empty_name_is_refused(self):
        with self.assertRaises(ValueError):
            await self.store.add_supplier("   ")

    async def test_rename(self):
        made = await self.store.add_supplier("Старое имя")
        self.assertTrue(await self.store.rename_supplier(made.id, "Новое имя"))
        self.assertIsNotNone(await self.store.find_supplier("новое имя"))
        self.assertIsNone(await self.store.find_supplier("Старое имя"))

    async def test_list_is_sorted_by_creation(self):
        for name in ("Первый", "Второй", "Третий"):
            await self.store.add_supplier(name)
        self.assertEqual([s.name for s in await self.store.list_suppliers()],
                         ["Первый", "Второй", "Третий"])

    async def test_delete_refuses_while_signatures_exist(self):
        """Иначе сигнатуры и файлы остались бы сиротами. Для дублей есть слияние."""
        made = await self.store.add_supplier("Монарх")
        await self.store.add_signature(made.id, "sig-1")
        self.assertFalse(await self.store.delete_supplier(made.id))
        self.assertIsNotNone(await self.store.get_supplier(made.id))

    async def test_empty_supplier_is_deleted(self):
        made = await self.store.add_supplier("Пустой")
        self.assertTrue(await self.store.delete_supplier(made.id))
        self.assertIsNone(await self.store.get_supplier(made.id))


class SignatureTest(Base):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.monarh = await self.store.add_supplier("Монарх")
        self.most = await self.store.add_supplier("Most Flooring")

    async def test_one_supplier_has_several_signatures(self):
        """Поставщик дробит прайс по типам товаров — это норма, а не ошибка."""
        await self.store.add_signature(self.monarh.id, "sig-lam", purpose="ламинат")
        await self.store.add_signature(self.monarh.id, "sig-tile", purpose="плитка")
        self.assertEqual(len(await self.store.list_signatures(self.monarh.id)), 2)

    async def test_same_signature_under_two_suppliers(self):
        """Скелеты могут совпасть случайно: это ДВЕ записи у двух владельцев (§2.2)."""
        await self.store.add_signature(self.monarh.id, "same")
        await self.store.add_signature(self.most.id, "same")
        owners = await self.store.find_signatures("same")
        self.assertEqual(len(owners), 2)
        self.assertEqual({o.supplier_id for o in owners}, {self.monarh.id, self.most.id})

    async def test_repeat_updates_last_seen_instead_of_duplicating(self):
        first = await self.store.add_signature(self.monarh.id, "sig", sample_name="прайс.xlsx")
        again = await self.store.add_signature(self.monarh.id, "sig")
        self.assertEqual(first.id, again.id)
        self.assertEqual(len(await self.store.list_signatures(self.monarh.id)), 1)
        self.assertEqual(again.first_seen, first.first_seen)

    async def test_repeat_keeps_known_fields(self):
        """Повторная встреча без назначения не должна стирать уже известное."""
        await self.store.add_signature(self.monarh.id, "sig", purpose="ламинат")
        again = await self.store.add_signature(self.monarh.id, "sig")
        self.assertEqual(again.purpose, "ламинат")

    async def test_move_to_another_supplier(self):
        sig = await self.store.add_signature(self.monarh.id, "sig")
        self.assertTrue(await self.store.move_signature(sig.id, self.most.id))
        self.assertEqual(await self.store.list_signatures(self.monarh.id), [])
        self.assertEqual(len(await self.store.list_signatures(self.most.id)), 1)

    async def test_move_onto_an_existing_twin_merges_instead_of_duplicating(self):
        """Пара (поставщик, сигнатура) обязана остаться одна."""
        src = await self.store.add_signature(self.monarh.id, "same")
        dst = await self.store.add_signature(self.most.id, "same")
        await self.store.add_price_file(src.id, "a.xlsx", "/p/a.xlsx")

        await self.store.move_signature(src.id, self.most.id)

        self.assertEqual(len(await self.store.list_signatures(self.most.id)), 1)
        self.assertEqual(len(await self.store.list_price_files(dst.id)), 1)

    async def test_delete_signature_takes_its_file_records(self):
        sig = await self.store.add_signature(self.monarh.id, "sig")
        await self.store.add_price_file(sig.id, "a.xlsx", "/p/a.xlsx")
        self.assertTrue(await self.store.delete_signature(sig.id))
        self.assertEqual(await self.store.list_price_files(sig.id), [])


class PriceFileTest(Base):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        supplier = await self.store.add_supplier("Монарх")
        self.sig = await self.store.add_signature(supplier.id, "sig")

    async def test_added_and_listed(self):
        await self.store.add_price_file(self.sig.id, "прайс.xlsx", "/p/1.xlsx",
                                        received_at="2026-09-13T10:00:00+00:00")
        files = await self.store.list_price_files(self.sig.id)
        self.assertEqual(files[0].filename, "прайс.xlsx")
        self.assertEqual(files[0].received_at, "2026-09-13T10:00:00+00:00")

    async def test_same_path_is_not_registered_twice(self):
        a = await self.store.add_price_file(self.sig.id, "п.xlsx", "/p/1.xlsx")
        b = await self.store.add_price_file(self.sig.id, "п.xlsx", "/p/1.xlsx")
        self.assertEqual(a.id, b.id)

    async def test_known_paths_for_orphan_sweep(self):
        await self.store.add_price_file(self.sig.id, "п.xlsx", "/p/1.xlsx")
        self.assertEqual(await self.store.known_paths(), {"/p/1.xlsx"})
        self.assertTrue(await self.store.path_is_registered("/p/1.xlsx"))
        self.assertFalse(await self.store.path_is_registered("/p/нет.xlsx"))

    async def test_delete_returns_the_path_but_not_the_file(self):
        """Судьбу файла на диске решает уборка сирот, а не справочник."""
        made = await self.store.add_price_file(self.sig.id, "п.xlsx", "/p/1.xlsx")
        self.assertEqual(await self.store.delete_price_file(made.id), "/p/1.xlsx")
        self.assertIsNone(await self.store.delete_price_file(made.id))


class MergeTest(Base):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.dup = await self.store.add_supplier("Монарх Логистик ООО")
        self.main = await self.store.add_supplier("Монарх")

    async def test_signatures_and_files_move_source_destroyed(self):
        sig = await self.store.add_signature(self.dup.id, "sig-lam", purpose="ламинат")
        await self.store.add_price_file(sig.id, "a.xlsx", "/p/a.xlsx")

        res = await self.store.merge_suppliers(self.dup.id, self.main.id)

        self.assertEqual((res.moved, res.absorbed, res.files), (1, 0, 1))
        self.assertTrue(res.removed)
        self.assertIsNone(await self.store.get_supplier(self.dup.id))
        moved = await self.store.list_signatures(self.main.id)
        self.assertEqual(len(moved), 1)
        self.assertEqual(len(await self.store.list_price_files(moved[0].id)), 1)

    async def test_matching_signature_is_absorbed_not_duplicated(self):
        src = await self.store.add_signature(self.dup.id, "same")
        dst = await self.store.add_signature(self.main.id, "same")
        await self.store.add_price_file(src.id, "a.xlsx", "/p/a.xlsx")
        await self.store.add_price_file(dst.id, "b.xlsx", "/p/b.xlsx")

        res = await self.store.merge_suppliers(self.dup.id, self.main.id)

        self.assertEqual((res.moved, res.absorbed), (0, 1))
        self.assertEqual(len(await self.store.list_signatures(self.main.id)), 1)
        self.assertEqual(len(await self.store.list_price_files(dst.id)), 2)

    async def test_empty_source_is_still_destroyed(self):
        res = await self.store.merge_suppliers(self.dup.id, self.main.id)
        self.assertEqual((res.moved, res.absorbed, res.files), (0, 0, 0))
        self.assertTrue(res.removed)

    async def test_merging_into_itself_is_refused(self):
        with self.assertRaises(ValueError):
            await self.store.merge_suppliers(self.dup.id, self.dup.id)

    async def test_unknown_target_is_refused(self):
        with self.assertRaises(ValueError):
            await self.store.merge_suppliers(self.dup.id, 9999)


class CountsTest(Base):

    async def test_counts_signatures_and_files(self):
        one = await self.store.add_supplier("Первый")
        two = await self.store.add_supplier("Второй")
        a = await self.store.add_signature(one.id, "s1")
        await self.store.add_signature(one.id, "s2")
        await self.store.add_price_file(a.id, "a.xlsx", "/p/a.xlsx")
        await self.store.add_price_file(a.id, "b.xlsx", "/p/b.xlsx")

        counts = await self.store.supplier_counts()
        self.assertEqual(counts[one.id], (2, 2))
        self.assertNotIn(two.id, counts)          # без сигнатур строки нет вовсе


class ViewTest(unittest.TestCase):
    """Формат вывода: дата по спеке — дд.мм.гг чч.мм."""

    def test_datetime_format(self):
        from datetime import datetime, timedelta, timezone
        stamp = datetime(2026, 9, 13, 14, 32, tzinfo=timezone(timedelta(hours=0)))
        got = view.human_dt(stamp.isoformat())
        self.assertRegex(got, r"^\d{2}\.\d{2}\.\d{2} \d{2}\.\d{2}$")

    def test_broken_and_empty_dates_do_not_crash(self):
        self.assertEqual(view.human_dt(None), "")
        self.assertEqual(view.human_dt(""), "")
        self.assertEqual(view.human_dt("не дата"), "")

    def test_empty_catalogue_explains_how_it_fills(self):
        text = view.render_suppliers([])
        self.assertIn("пуст", text)
        self.assertIn("подписи к файлу", text)

    def test_suppliers_are_numbered_from_one(self):
        from src.storage.suppliers import Supplier
        rows = [Supplier(7, "Монарх", "монарх", "2026-09-13T10:00:00+00:00"),
                Supplier(3, "Most", "most", "2026-09-13T11:00:00+00:00")]
        text = view.render_suppliers(rows, {7: (2, 5), 3: (1, 1)})
        self.assertIn("1. Монарх", text)
        self.assertIn("2. Most", text)
        self.assertIn("2 сигнатуры, 5 файлов", text)
        self.assertIn("1 сигнатура, 1 файл", text)

    def test_plural_forms(self):
        from src.storage.suppliers import Supplier
        rows = [Supplier(1, "A", "a", "2026-09-13T10:00:00+00:00")]
        self.assertIn("5 сигнатур", view.render_suppliers(rows, {1: (5, 0)}))
        self.assertIn("0 файлов", view.render_suppliers(rows, {1: (5, 0)}))


class FakeMessage:
    def __init__(self):
        self.sent: list[str] = []

    async def answer(self, text, **kw):
        self.sent.append(text)
        return self

    @property
    def last(self) -> str:
        return self.sent[-1] if self.sent else ""


class Args:
    def __init__(self, args=None):
        self.args = args


class CommandTest(Base):
    """Команды целиком: они и есть то, чем админ правит справочник."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from src.bot import catalog_handlers as ch
        self.ch = ch
        self.msg = FakeMessage()

    async def test_empty_catalogue_tells_how_it_fills(self):
        await self.ch.cmd_suppliers(self.msg, self.store, True)
        self.assertIn("пуст", self.msg.last)

    async def test_add_then_list(self):
        await self.ch.cmd_supplier_add(self.msg, Args("Монарх Логистик"), self.store, True)
        await self.ch.cmd_suppliers(self.msg, self.store, True)
        self.assertIn("1. Монарх Логистик", self.msg.last)

    async def test_add_twice_says_it_exists(self):
        await self.ch.cmd_supplier_add(self.msg, Args("Монарх"), self.store, True)
        await self.ch.cmd_supplier_add(self.msg, Args("  монарх  "), self.store, True)
        self.assertIn("уже есть", self.msg.last)
        self.assertEqual(len(await self.store.list_suppliers()), 1)

    async def test_rename_by_number(self):
        await self.store.add_supplier("Старое")
        await self.ch.cmd_supplier_rename(self.msg, Args("1 Новое имя"), self.store, True)
        self.assertIsNotNone(await self.store.find_supplier("Новое имя"))

    async def test_delete_refusal_suggests_merge(self):
        made = await self.store.add_supplier("Монарх")
        await self.store.add_signature(made.id, "sig")
        await self.ch.cmd_supplier_delete(self.msg, Args("1"), self.store, True)
        self.assertIn("удалять нельзя", self.msg.last)
        self.assertIn("/supplier_merge", self.msg.last)

    async def test_merge_by_numbers(self):
        dup = await self.store.add_supplier("Дубль")
        await self.store.add_supplier("Основной")
        await self.store.add_signature(dup.id, "sig")

        await self.ch.cmd_supplier_merge(self.msg, Args("1 2"), self.store, True)

        self.assertIn("влит", self.msg.last)
        self.assertEqual(len(await self.store.list_suppliers()), 1)

    async def test_merge_needs_two_numbers(self):
        await self.store.add_supplier("Один")
        await self.ch.cmd_supplier_merge(self.msg, Args("1"), self.store, True)
        self.assertIn("два номера", self.msg.last)

    async def test_bad_number_does_not_touch_anything(self):
        await self.store.add_supplier("Монарх")
        await self.ch.cmd_supplier_delete(self.msg, Args("99"), self.store, True)
        self.assertIn("Нужен номер", self.msg.last)
        self.assertEqual(len(await self.store.list_suppliers()), 1)

    async def test_signature_numbers_are_global_under_filter(self):
        """Фильтр прячет строки, но НЕ сдвигает номера.

        Иначе `/signature_delete 2` в отфильтрованном виде убрал бы не ту сигнатуру,
        которую админ видит под этим номером.
        """
        one = await self.store.add_supplier("Первый")
        two = await self.store.add_supplier("Второй")
        await self.store.add_signature(one.id, "s1", purpose="ламинат")
        await self.store.add_signature(two.id, "s2", purpose="плитка")
        await self.store.add_signature(two.id, "s3", purpose="паркет")

        await self.ch.cmd_signatures(self.msg, Args("2"), self.store, True)   # только «Второй»

        self.assertIn("2. плитка", self.msg.last)
        self.assertIn("3. паркет", self.msg.last)
        self.assertNotIn("1. ", self.msg.last)      # первая сигнатура чужая и скрыта

    async def test_signature_move_by_numbers(self):
        one = await self.store.add_supplier("Первый")
        two = await self.store.add_supplier("Второй")
        sig = await self.store.add_signature(one.id, "s1", purpose="ламинат")

        await self.ch.cmd_signature_move(self.msg, Args("1 2"), self.store, True)

        self.assertEqual(await self.store.list_signatures(one.id), [])
        moved = await self.store.list_signatures(two.id)
        self.assertEqual(moved[0].id, sig.id)

    async def test_non_admin_is_refused_everywhere(self):
        for call, args in ((self.ch.cmd_suppliers, None),
                           (self.ch.cmd_supplier_add, Args("Имя")),
                           (self.ch.cmd_supplier_merge, Args("1 2")),
                           (self.ch.cmd_signature_delete, Args("1"))):
            msg = FakeMessage()
            if args is None:
                await call(msg, self.store, False)
            else:
                await call(msg, args, self.store, False)
            self.assertIn("только администратору", msg.last)


if __name__ == "__main__":
    unittest.main()
