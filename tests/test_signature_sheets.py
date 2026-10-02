"""Указание админа «разбирать только эти листы» (решение админа 02.10.2026).

Экономия токенов: у FLOOR SERVICE четырнадцать листов, по делу два-три, а разбор каждого
лишнего стоит и токенов, и кругов цикла. Указание живёт у СИГНАТУРЫ формата — следующий
прайс того же поставщика придёт с теми же листами.

После разбора агент обязан сказать, что смотрел и что пропустил: по списку задач не видно,
обошли прайс целиком или треть его.
"""
import tempfile
import unittest
from pathlib import Path

from src.bot import catalog_handlers as ch
from src.model.task_builder import TaskBuilderTools, sheets_report
from src.storage.suppliers import SupplierStore


class Sheet:
    def __init__(self, name):
        self.name = name
        self.rows = [["Артикул", "Цена"], ["A1", "100"]]


ALL = ["ИЗМЕНЕНИЯ", "АКЦИИ", "ЛАМИНАТ", "SPC", "КЛЕЙ"]


def tools(only="", sheets=None):
    t = TaskBuilderTools(b"x", "Прайс.xlsx", only_sheets=only)
    t._sheets = t._pick([Sheet(n) for n in (sheets or ALL)])
    return t


class PickTest(unittest.TestCase):

    def test_without_instruction_all_sheets(self):
        t = tools()
        self.assertEqual([s.name for s in t.sheets], ALL)
        self.assertEqual(t.skipped_sheets, [])

    def test_only_named_sheets_remain(self):
        t = tools("ЛАМИНАТ, SPC")
        self.assertEqual([s.name for s in t.sheets], ["ЛАМИНАТ", "SPC"])
        self.assertEqual(t.skipped_sheets, ["ИЗМЕНЕНИЯ", "АКЦИИ", "КЛЕЙ"])

    def test_case_and_spaces_do_not_matter(self):
        """Админ набирает имена руками."""
        t = tools("  ламинат ,spc  ")
        self.assertEqual([s.name for s in t.sheets], ["ЛАМИНАТ", "SPC"])

    def test_unknown_names_are_remembered(self):
        t = tools("ЛАМИНАТ, ОБОИ")
        self.assertEqual([s.name for s in t.sheets], ["ЛАМИНАТ"])
        self.assertEqual(t.unknown_sheets, ["ОБОИ"])

    def test_empty_intersection_means_everything(self):
        """Поставщик переименовал листы — указание устарело. Разобрав ноль листов, мы
        получили бы ноль задач, а ноль задач в этой модели значит «расхождений нет»:
        тихо выдать «работы нет» вместо «не нашёл названных листов» — худшее из возможного.
        """
        t = tools("ОБОИ, ПЛИТКА")
        self.assertEqual([s.name for s in t.sheets], ALL)
        self.assertEqual(t.skipped_sheets, [])
        self.assertEqual(t.unknown_sheets, ["ОБОИ", "ПЛИТКА"])


class ReadTest(unittest.TestCase):

    def test_excluded_sheet_is_named_not_substituted(self):
        """Молча подменив лист, мы заставили бы агента искать ламинат в «АКЦИЯХ»."""
        t = tools("ЛАМИНАТ")
        answer = t._read({"sheet": "АКЦИИ"})
        self.assertIn("исключён указанием", answer)
        self.assertIn("ЛАМИНАТ", answer)

    def test_read_sheets_are_tracked(self):
        t = tools()
        t._read({"sheet": "ЛАМИНАТ"})
        t._read({"sheet": "SPC"})
        t._read({"sheet": "ЛАМИНАТ"})
        self.assertEqual(t.read_sheets, ["ЛАМИНАТ", "SPC"], "без повторов")


class ReportTest(unittest.TestCase):

    def test_three_groups(self):
        t = tools("ЛАМИНАТ, SPC, КЛЕЙ")
        t._read({"sheet": "ЛАМИНАТ"})
        text = sheets_report(t)
        self.assertIn("Проанализировал листы: ЛАМИНАТ.", text)
        self.assertIn("Исключены указанием по этому формату: ИЗМЕНЕНИЯ, АКЦИИ.", text)
        self.assertIn("Остались нетронутыми: SPC, КЛЕЙ.", text)

    def test_untouched_is_separate_from_excluded(self):
        """«Исключил я» и «агент сам не открыл» — разные вещи: второе бывает ошибкой
        (прайс на три листа, агент обошёл один — бой 24.09.2026)."""
        t = tools()
        t._read({"sheet": "ЛАМИНАТ"})
        text = sheets_report(t)
        self.assertNotIn("Исключены", text)
        self.assertIn("Остались нетронутыми", text)

    def test_nothing_opened_is_said_out_loud(self):
        self.assertIn("Ни одного листа не открыл", sheets_report(tools()))

    def test_total_mismatch_says_the_limit_was_dropped(self):
        t = tools("ОБОИ")
        t._read({"sheet": "ЛАМИНАТ"})
        text = sheets_report(t)
        self.assertIn("которых в файле нет: ОБОИ", text)
        self.assertIn("не применялось", text)

    def test_partial_mismatch_does_not_claim_the_limit_was_dropped(self):
        """Часть названий совпала — ограничение ПРИМЕНИЛОСЬ, и говорить обратное значило
        бы соврать ровно о том, что админ и проверяет."""
        t = tools("ЛАМИНАТ, ОБОИ")
        t._read({"sheet": "ЛАМИНАТ"})
        text = sheets_report(t)
        self.assertIn("которых в файле нет: ОБОИ", text)
        self.assertIn("ограничение применено", text)
        self.assertNotIn("не применялось", text)

    def test_empty_groups_are_not_printed(self):
        t = tools("ЛАМИНАТ", sheets=["ЛАМИНАТ"])
        t._read({"sheet": "ЛАМИНАТ"})
        text = sheets_report(t)
        self.assertEqual(text, "Проанализировал листы: ЛАМИНАТ.")


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def answer(self, text, reply_markup=None):
        self.sent.append(text)
        return self

    @property
    def text(self):
        return "\n".join(self.sent)


class Args:
    def __init__(self, args=""):
        self.args = args


class StoreTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = SupplierStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        supplier = await self.store.add_supplier("FLOOR SERVICE")
        self.sig = await self.store.add_signature(supplier.id, "hash-1",
                                                  sample_name="прайс.xlsx")

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_set_and_read_by_hash(self):
        await self.store.set_signature_sheets(self.sig.id, "ЛАМИНАТ, SPC")
        self.assertEqual(await self.store.sheets_for("hash-1"), "ЛАМИНАТ, SPC")

    async def test_kept_as_typed(self):
        """Показывать надо то, что задал админ, иначе он не узнает своё указание."""
        await self.store.set_signature_sheets(self.sig.id, "Ламинат , SPC")
        self.assertEqual(await self.store.sheets_for("hash-1"), "Ламинат , SPC")

    async def test_empty_clears(self):
        await self.store.set_signature_sheets(self.sig.id, "ЛАМИНАТ")
        await self.store.set_signature_sheets(self.sig.id, "")
        self.assertEqual(await self.store.sheets_for("hash-1"), "")

    async def test_unknown_signature(self):
        self.assertEqual(await self.store.sheets_for("нет-такого"), "")
        self.assertEqual(await self.store.sheets_for(""), "")

    async def test_shown_in_the_listing(self):
        from src.price_tool import catalog_view as view

        await self.store.set_signature_sheets(self.sig.id, "ЛАМИНАТ")
        rows = [(1, s) for s in await self.store.list_signatures()]
        self.assertIn("разбираем только листы: ЛАМИНАТ", view.render_signatures(rows))

    async def test_command_sets_it(self):
        msg = FakeMessage()
        await ch.cmd_signature_sheets(msg, Args("1 ЛАМИНАТ, SPC"), self.store,
                                      is_admin=True)
        self.assertIn("только листы — ЛАМИНАТ, SPC", msg.text)
        self.assertEqual(await self.store.sheets_for("hash-1"), "ЛАМИНАТ, SPC")

    async def test_command_without_sheets_clears(self):
        await self.store.set_signature_sheets(self.sig.id, "ЛАМИНАТ")
        msg = FakeMessage()
        await ch.cmd_signature_sheets(msg, Args("1"), self.store, is_admin=True)
        self.assertIn("Ограничение снято", msg.text)
        self.assertEqual(await self.store.sheets_for("hash-1"), "")

    async def test_command_needs_a_number(self):
        msg = FakeMessage()
        await ch.cmd_signature_sheets(msg, Args(""), self.store, is_admin=True)
        self.assertIn("Нужен номер", msg.text)

    async def test_manager_cannot(self):
        msg = FakeMessage()
        await ch.cmd_signature_sheets(msg, Args("1 ЛАМИНАТ"), self.store, is_admin=False)
        self.assertIn("только администратору", msg.text)
        self.assertEqual(await self.store.sheets_for("hash-1"), "")


if __name__ == "__main__":
    unittest.main()
