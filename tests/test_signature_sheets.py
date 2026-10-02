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


def tools(only=None, sheets=None):
    t = TaskBuilderTools(b"x", "Прайс.xlsx", only_sheets=only)
    t._sheets = t._pick([Sheet(n) for n in (sheets or ALL)])
    return t


class PickTest(unittest.TestCase):

    def test_no_instruction_at_all_means_all_sheets(self):
        """`None` — выбором листов никто не управляет (так зовут из кода, которому до
        листов дела нет). Это НЕ то же, что «админ ничего не отметил»."""
        t = tools()
        self.assertEqual([s.name for s in t.sheets], ALL)
        self.assertEqual(t.skipped_sheets, [])
        self.assertEqual(t.pick_problem, "")

    def test_nothing_ticked_means_nothing_parsed(self):
        """Решение админа 02.10.2026: не отмечено — не разбираем. Обратное умолчание
        стоило бы полного разбора каждого нового файла ($5.83 на FLOOR SERVICE)."""
        t = tools(only="")
        self.assertEqual(t.sheets, [])
        self.assertEqual(t.pick_problem, "листы не отмечены")
        self.assertEqual(t.skipped_sheets, ALL)

    def test_only_named_sheets_remain(self):
        t = tools("ЛАМИНАТ, SPC")
        self.assertEqual([s.name for s in t.sheets], ["ЛАМИНАТ", "SPC"])
        self.assertEqual(t.skipped_sheets, ["ИЗМЕНЕНИЯ", "АКЦИИ", "КЛЕЙ"])

    def test_case_and_spaces_do_not_matter(self):
        """Админ набирает имена руками."""
        t = tools("  ламинат ,spc  ")
        self.assertEqual([s.name for s in t.sheets], ["ЛАМИНАТ", "SPC"])

    def test_date_in_the_sheet_name_does_not_lose_the_sheet(self):
        """«Прайс от 20.04.2026» и «Прайс от 01.10.2026» — один лист (бой 02.10.2026).

        Отметка хранится именем, а поставщик дописывает в имя дату. Точное сравнение
        теряло бы главный лист каждый месяц — и теряло бы ТИХО: формат опознан, лист в
        книге есть, в отчёте он числится «пропущенным по указанию админа».
        """
        t = tools("Прайс от 20.04.2026, SPC", sheets=["Прайс от 01.10.2026", "SPC", "КЛЕЙ"])
        self.assertEqual([s.name for s in t.sheets], ["Прайс от 01.10.2026", "SPC"])
        self.assertEqual(t.unknown_sheets, [])

    def test_unknown_names_are_remembered(self):
        t = tools("ЛАМИНАТ, ОБОИ")
        self.assertEqual([s.name for s in t.sheets], ["ЛАМИНАТ"])
        self.assertEqual(t.unknown_sheets, ["ОБОИ"])

    def test_stale_marks_parse_nothing_and_say_so(self):
        """Поставщик переименовал листы — отмеченных в файле больше нет. Разбирать нечего,
        и сказать об этом надо громко: иначе «задач нет» прочтётся как «расхождений нет»."""
        t = tools("ОБОИ, ПЛИТКА")
        self.assertEqual(t.sheets, [])
        self.assertEqual(t.pick_problem, "ни один отмеченный лист не найден в файле")
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

    def test_nothing_ticked_report_tells_what_to_do(self):
        """Список задач пуст, и без объяснения пустота читается как сделанная работа."""
        text = sheets_report(tools(only=""))
        self.assertIn("Листы не отмечены — задачи не собирал", text)
        self.assertIn("В прайсе есть листы: ИЗМЕНЕНИЯ", text)
        self.assertIn("Обновить задачи", text)

    def test_stale_marks_report_blames_the_renaming(self):
        text = sheets_report(tools("ОБОИ"))
        self.assertIn("Ни один отмеченный лист не найден", text)
        self.assertIn("Отмечены: ОБОИ", text)
        self.assertIn("переименовал", text)

    def test_partial_mismatch_is_only_a_warning(self):
        """Часть отмеченных нашлась — разбор состоялся, и это не повод бить тревогу."""
        t = tools("ЛАМИНАТ, ОБОИ")
        t._read({"sheet": "ЛАМИНАТ"})
        text = sheets_report(t)
        self.assertIn("Проанализировал листы: ЛАМИНАТ.", text)
        self.assertIn("которых в файле нет: ОБОИ", text)

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

    async def test_dash_clears(self):
        await self.store.set_signature_sheets(self.sig.id, "ЛАМИНАТ")
        msg = FakeMessage()
        await ch.cmd_signature_sheets(msg, Args("1 -"), self.store, is_admin=True)
        self.assertIn("Ограничение снято", msg.text)
        self.assertEqual(await self.store.sheets_for("hash-1"), "")

    async def test_without_arguments_it_shows_what_to_choose_from(self):
        """Имена листов знает только файл, и набирать их по памяти — верный способ
        промахнуться: промах снимает ограничение целиком, то есть молча возвращает разбор
        к полному."""
        await self.store.add_signature(self.sig.supplier_id, "hash-1",
                                       sheet_list="ЛАМИНАТ, SPC, КЛЕЙ")
        await self.store.set_signature_sheets(self.sig.id, "ЛАМИНАТ")
        msg = FakeMessage()
        await ch.cmd_signature_sheets(msg, Args("1"), self.store, is_admin=True)
        self.assertIn("Листы последнего файла: ЛАМИНАТ, SPC, КЛЕЙ", msg.text)
        self.assertIn("Разбираем только: ЛАМИНАТ", msg.text)
        self.assertEqual(await self.store.sheets_for("hash-1"), "ЛАМИНАТ",
                         "показ ничего не меняет")

    async def test_without_arguments_and_without_a_file_yet(self):
        msg = FakeMessage()
        await ch.cmd_signature_sheets(msg, Args("1"), self.store, is_admin=True)
        self.assertIn("Листы пока неизвестны", msg.text)
        self.assertIn("Ограничения нет", msg.text)

    async def test_sheet_names_are_remembered_per_signature(self):
        await self.store.add_signature(self.sig.supplier_id, "hash-1",
                                       sheet_list="ЛАМИНАТ, SPC")
        rows = await self.store.list_signatures()
        self.assertEqual(rows[0].sheet_list, "ЛАМИНАТ, SPC")

    async def test_fresh_file_replaces_the_sheet_list(self):
        """Поставщик добавляет и убирает листы: накопленный список однажды предложил бы
        выбрать тот, которого в прайсе давно нет."""
        await self.store.add_signature(self.sig.supplier_id, "hash-1",
                                       sheet_list="ЛАМИНАТ, SPC")
        await self.store.add_signature(self.sig.supplier_id, "hash-1",
                                       sheet_list="ЛАМИНАТ")
        rows = await self.store.list_signatures()
        self.assertEqual(rows[0].sheet_list, "ЛАМИНАТ")

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


class MirrorTest(unittest.IsolatedAsyncioTestCase):
    """Зеркало листов для формы 1С: строка на лист, флажок — отметка админа."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = SupplierStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        supplier = await self.store.add_supplier("FLOOR SERVICE")
        self.sig = await self.store.add_signature(
            supplier.id, "hash-1", sample_name="прайс.xlsx",
            sheet_list="ИЗМЕНЕНИЯ, ЛАМИНАТ, SPC")

    async def asyncTearDown(self):
        self._dir.cleanup()

    def provider(self):
        from src.onec.model_provider import OnecProvider

        class Service:
            prices = []

            def lock_of(self, _id):
                return None

        return OnecProvider(onec=None, service=Service(), suppliers=self.store)

    async def test_row_per_sheet_with_flags(self):
        await self.store.set_signature_sheets(self.sig.id, "ЛАМИНАТ")
        rows = await self.provider().sheets_snapshot()
        self.assertEqual([(r["sheet"], r["parse"]) for r in rows],
                         [("ИЗМЕНЕНИЯ", False), ("ЛАМИНАТ", True), ("SPC", False)])
        self.assertTrue(all(r["signature"] == "hash-1" for r in rows))

    async def test_date_in_the_sheet_name_keeps_the_flag(self):
        """Отметка хранится ИМЕНЕМ, а поставщик дописывает в имя дату (бой 02.10.2026).

        «Прайс от 20.04.2026» в апреле и «Прайс от 01.10.2026» в октябре — один и тот же
        лист. Точное сравнение снимало бы флажок каждый месяц, и админ отмечал бы заново
        то, что уже отмечал, не понимая, почему выбор не держится.
        """
        await self.store.add_signature(self.sig.supplier_id, "hash-1",
                                       sheet_list="Прайс от 01.10.2026, SPC")
        await self.store.set_signature_sheets(self.sig.id, "Прайс от 20.04.2026")
        rows = await self.provider().sheets_snapshot()
        self.assertEqual([(r["sheet"], r["parse"]) for r in rows],
                         [("Прайс от 01.10.2026", True), ("SPC", False)])

    async def test_supplier_goes_by_code_not_only_by_name(self):
        """В 1С колонка «Поставщик» — ССЫЛКА на зеркало справочника (правка админа
        02.10.2026), и элемент там опознаётся по коду: по имени зеркало плодило бы дубль
        на каждое переименование."""
        rows = await self.provider().sheets_snapshot()
        self.assertTrue(all(r["supplier_code"] for r in rows))

    async def test_order_comes_from_the_file_not_the_alphabet(self):
        """Админ ищет лист глазами там, где он стоит в книге."""
        rows = await self.provider().sheets_snapshot()
        self.assertEqual([r["order"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[0]["sheet"], "ИЗМЕНЕНИЯ")

    async def test_format_without_a_file_is_not_shown(self):
        """Пустая строка в таблице только мешала бы: выбирать не из чего."""
        supplier = await self.store.add_supplier("Новый")
        await self.store.add_signature(supplier.id, "hash-2")
        rows = await self.provider().sheets_snapshot()
        self.assertEqual({r["signature"] for r in rows}, {"hash-1"})

    async def test_without_a_catalogue_it_is_empty(self):
        from src.onec.model_provider import OnecProvider

        class Service:
            prices = []

            def lock_of(self, _id):
                return None

        provider = OnecProvider(onec=None, service=Service())
        self.assertEqual(await provider.sheets_snapshot(), [])


class CommandTest(unittest.IsolatedAsyncioTestCase):
    """Команда из формы 1С: отмеченные листы приезжают в модель."""

    async def asyncSetUp(self):
        from src.model.service import PriceListService
        from src.storage.model_store import ModelStore

        self._dir = tempfile.TemporaryDirectory()
        db = Path(self._dir.name) / "t.db"
        self.store = SupplierStore(db)
        await self.store.init()
        model_store = ModelStore(db)
        await model_store.init()
        supplier = await self.store.add_supplier("FLOOR SERVICE")
        self.sig = await self.store.add_signature(supplier.id, "hash-1",
                                                  sheet_list="ЛАМИНАТ, SPC")
        self.model = PriceListService(model_store, self.store,
                                      save_file=lambda c, n: None)
        await self.model.load()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def send(self, payload):
        from src.model.commands import Command, CommandKind

        await self.model.apply(Command(kind=CommandKind.SET_SIGNATURE_SHEETS,
                                       source="1c", actor="1c:Саша", payload=payload))

    async def test_list_of_sheets_is_stored(self):
        await self.send({"signature": "hash-1", "sheets": ["ЛАМИНАТ", "SPC"]})
        self.assertEqual(await self.store.sheets_for("hash-1"), "ЛАМИНАТ, SPC")

    async def test_string_is_accepted_too(self):
        """Telegram шлёт строкой, форма — списком: принимаем оба, иначе один из визуалов
        пришлось бы учить формату другого."""
        await self.send({"signature": "hash-1", "sheets": "ЛАМИНАТ"})
        self.assertEqual(await self.store.sheets_for("hash-1"), "ЛАМИНАТ")

    async def test_empty_selection_is_stored_as_empty(self):
        """Снять все флажки — законное действие: оно означает «этот формат не разбирать»."""
        await self.store.set_signature_sheets(self.sig.id, "ЛАМИНАТ")
        await self.send({"signature": "hash-1", "sheets": []})
        self.assertEqual(await self.store.sheets_for("hash-1"), "")

    async def test_unknown_signature_is_refused(self):
        await self.send({"signature": "нет-такого", "sheets": ["ЛАМИНАТ"]})
        self.assertEqual(await self.store.sheets_for("hash-1"), "")

    async def test_without_signature_is_refused(self):
        await self.send({"sheets": ["ЛАМИНАТ"]})
        self.assertEqual(await self.store.sheets_for("hash-1"), "")


class BackfillTest(unittest.IsolatedAsyncioTestCase):
    """Дозаполнение листов у форматов, заведённых до появления этой памяти.

    Файлы прайсов лежат на диске, и сигнатура у формата от них же и посчитана: значит имена
    листов не потеряны — их достаточно прочитать (бой 02.10.2026, пять форматов).
    """

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.store = SupplierStore(self.root / "t.db")
        await self.store.init()
        supplier = await self.store.add_supplier("FLOOR SERVICE")
        self.sig = await self.store.add_signature(supplier.id, "hash-1",
                                                  sample_name="прайс.xlsx")

    async def asyncTearDown(self):
        self._dir.cleanup()

    def workbook(self, *names):
        import openpyxl

        wb = openpyxl.Workbook()
        wb.active.title = names[0]
        for extra in names[1:]:
            wb.create_sheet(extra)
        path = self.root / "прайс.xlsx"
        wb.save(path)
        return path

    async def add_file(self, path, received_at="2026-09-01T00:00:00"):
        return await self.store.add_price_file(self.sig.id, path.name, str(path),
                                              received_at=received_at)

    async def run_fill(self):
        from src.model.sheet_backfill import fill_sheet_lists

        return await fill_sheet_lists(self.store)

    async def test_names_are_read_from_the_file(self):
        await self.add_file(self.workbook("ЛАМИНАТ", "SPC"))
        self.assertEqual(await self.run_fill(), 1)
        self.assertEqual((await self.store.list_signatures())[0].sheet_list,
                         "ЛАМИНАТ, SPC")

    async def test_existing_list_is_not_touched(self):
        """Свежий приём всегда главнее починки: перезаписав, мы вернули бы устаревший
        список из старого файла."""
        await self.store.set_signature_sheets(self.sig.id, "ЛАМИНАТ")
        await self.store.add_signature(self.sig.supplier_id, "hash-1",
                                       sheet_list="ЛАМИНАТ, SPC")
        await self.add_file(self.workbook("СОВСЕМ", "ДРУГИЕ"))
        self.assertEqual(await self.run_fill(), 0)
        self.assertEqual((await self.store.list_signatures())[0].sheet_list,
                         "ЛАМИНАТ, SPC")

    async def test_newest_file_wins(self):
        """Поставщик добавляет и убирает листы: список месячной давности предложил бы
        выбрать тот, которого в прайсе давно нет."""
        import openpyxl

        old = self.root / "старый.xlsx"
        wb = openpyxl.Workbook()
        wb.active.title = "СТАРЫЙ"
        wb.save(old)
        await self.add_file(old, received_at="2026-01-01T00:00:00")
        await self.add_file(self.workbook("НОВЫЙ"), received_at="2026-09-09T00:00:00")

        await self.run_fill()
        self.assertEqual((await self.store.list_signatures())[0].sheet_list, "НОВЫЙ")

    async def test_missing_file_is_skipped_without_a_crash(self):
        await self.store.add_price_file(self.sig.id, "нет.xlsx",
                                        str(self.root / "нет.xlsx"))
        self.assertEqual(await self.run_fill(), 0)

    async def test_format_without_files_is_skipped(self):
        self.assertEqual(await self.run_fill(), 0)

    async def test_running_twice_is_free(self):
        await self.add_file(self.workbook("ЛАМИНАТ"))
        self.assertEqual(await self.run_fill(), 1)
        self.assertEqual(await self.run_fill(), 0)
