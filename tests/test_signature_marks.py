"""Бренды у сигнатуры: справочник, зеркало, команда, отчёт (решение админа 03.10.2026).

Второй список у формата, рядом с листами. К разбору уходит ПЕРЕСЕЧЕНИЕ отмеченного: выбор
листов бесполезен прайсу, у которого лист один, — у «Остатков» одна вкладка на 1286 строк и
23 бренда, из них 13 в справочнике 1С отсутствуют вовсе.

Проверяется то, что ломается тихо: решение админа при новом файле, бренд, исчезнувший из
файла, предложение марки (оно не вправе переписывать выбор человека) и пустое пересечение.
"""
import tempfile
import unittest
from pathlib import Path

from src.bot import catalog_handlers as ch
from src.model.brand_intake import propose_marks
from src.model.commands import Command, CommandKind
from src.storage.suppliers import SupplierStore


class Mark:
    """Марка 1С в том виде, в каком её отдаёт `selling_tm`."""

    def __init__(self, code, name, selling=True):
        self.code, self.name, self.selling = code, name, selling


MARKS = [Mark("000000265", "ABK"), Mark("000000285", "VitrA"),
         Mark("000000115", "Atlas Concorde / Атлас конкорд"),
         Mark("000000287", "LAMINAM"), Mark("000000141", "Imola")]


class ProposeTest(unittest.TestCase):

    def test_exact_name(self):
        self.assertEqual(propose_marks(["ABK"], MARKS)["ABK"], ("000000265", "ABK"))

    def test_bilingual_half(self):
        """В справочнике имя двуязычное, в прайсе — одна половина."""
        got = propose_marks(["Atlas Concorde"], MARKS)
        self.assertEqual(got["Atlas Concorde"][0], "000000115")

    def test_case_does_not_matter(self):
        self.assertIn("Laminam", propose_marks(["Laminam"], MARKS))

    def test_first_word_when_the_label_carries_a_product_kind(self):
        """«Italon Керамический гранит» и «Italon Настенная плитка» — два ярлыка одного
        бренда, и различает их вид товара, а не марка."""
        marks = MARKS + [Mark("000000900", "Italon")]
        got = propose_marks(["Italon Керамический гранит"], marks)
        self.assertEqual(got["Italon Керамический гранит"][0], "000000900")

    def test_unknown_brand_is_not_guessed(self):
        """Из 23 ярлыков боевого файла в справочнике нет тринадцати. Неверная марка хуже
        отсутствующей: по ней цены уехали бы ЧУЖОЙ ТМ."""
        self.assertEqual(propose_marks(["Dogma", "WOW", "CE.SI"], MARKS), {})

    def test_longer_label_is_not_glued_to_a_shorter_mark(self):
        """«Atlas Concorde Russia» — другое юрлицо, и приклеить его к «Atlas Concorde»
        значило бы приписать цены не тому."""
        self.assertNotIn("Atlas Concorde Russia",
                         propose_marks(["Atlas Concorde Russia"], MARKS))

    def test_two_candidates_mean_none(self):
        twins = [Mark("1", "Vintage"), Mark("2", "Vintage")]
        self.assertEqual(propose_marks(["Vintage"], twins), {})


class StoreTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = SupplierStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        supplier = await self.store.add_supplier("Керамика")
        self.sig = await self.store.add_signature(supplier.id, "hash-1",
                                                  sample_name="Остатки.xls")
        await self.store.remember_marks("hash-1", [
            ("ABK", 22, "000000265", "ABK"),
            ("VitrA", 202, "", ""),
            ("Dogma", 19, "", "")])

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_new_brands_arrive_unticked(self):
        """Обратное умолчание значило бы, что появившийся бренд молча уедет в разбор — за
        токены и с задачами по товарам, которых магазин может не возить."""
        self.assertEqual(await self.store.marks_wanted("hash-1"), [])

    async def test_order_is_the_file_order(self):
        self.assertEqual([m.brand for m in await self.store.marks_for("hash-1")],
                         ["ABK", "VitrA", "Dogma"])

    async def test_admin_choice_survives_the_next_file(self):
        """Файл того же формата приходит каждый месяц, а флажки админ расставил руками."""
        await self.store.set_marks_by_signature("hash-1", [
            {"brand": "ABK", "parse": True, "tm_code": "000000265",
             "tm_name": "ABK", "discount": 17.5}])

        await self.store.remember_marks("hash-1", [("ABK", 25, "ЧУЖОЙ", "ЧУЖАЯ"),
                                                   ("VitrA", 200, "", "")])

        abk = next(m for m in await self.store.marks_for("hash-1") if m.brand == "ABK")
        self.assertTrue(abk.parse)
        self.assertEqual(abk.discount, 17.5)
        self.assertEqual(abk.rows, 25)
        # Догадка кода не вправе переписывать выбор человека.
        self.assertEqual(abk.tm_code, "000000265")

    async def test_vanished_brand_keeps_its_state_with_zero_rows(self):
        """Удалив, потеряли бы флажок, скидку и марку, — а поставщик вернёт бренд
        следующим файлом. Ноль строк при этом ВИДЕН админу: это новость."""
        await self.store.set_marks_by_signature(
            "hash-1", [{"brand": "Dogma", "parse": True, "discount": 10}])
        await self.store.remember_marks("hash-1", [("ABK", 22, "", "")])

        dogma = next(m for m in await self.store.marks_for("hash-1") if m.brand == "Dogma")
        self.assertEqual(dogma.rows, 0)
        self.assertTrue(dogma.parse)
        self.assertEqual(dogma.discount, 10)

    async def test_proposed_mark_fills_an_empty_slot(self):
        await self.store.remember_marks("hash-1", [("VitrA", 202, "000000285", "VitrA")])
        vitra = next(m for m in await self.store.marks_for("hash-1") if m.brand == "VitrA")
        self.assertEqual(vitra.tm_code, "000000285")

    async def test_marks_survive_a_hash_recompute(self):
        """Таблица ключуется ХЕШОМ: оставшись под старым, выбор осиротел бы молча — ровно
        так уже терялся выбор листов (02.10.2026)."""
        await self.store.set_marks_by_signature(
            "hash-1", [{"brand": "ABK", "parse": True}])
        await self.store.rehash_signature(self.sig.id, "hash-2")

        self.assertEqual(await self.store.marks_wanted("hash-2"), ["ABK"])
        self.assertEqual(await self.store.marks_for("hash-1"), [])

    async def test_brand_column_and_currency_are_remembered(self):
        """Валюта одна на прайс, но ПОСЛЕДНЯЯ введённая живёт у формата — как подсказка
        форме: курс меняется каждый день, и считать по прошлому нельзя."""
        await self.store.set_signature_brand_col("hash-1", 5)
        await self.store.set_last_currency("hash-1", "978", "EUR", 98.4321)
        self.assertEqual(await self.store.brand_col_for("hash-1"), 5)
        self.assertEqual(await self.store.last_currency("hash-1"),
                         {"code": "978", "name": "EUR", "rate": 98.4321})

    async def test_without_a_currency_it_is_roubles(self):
        """Не указана — считаем рублём, как раньше."""
        self.assertEqual(await self.store.last_currency("hash-1"),
                         {"code": "", "name": "", "rate": None})

    async def test_unknown_signature_gives_nothing(self):
        self.assertEqual(await self.store.marks_for("нет-такого"), [])
        self.assertIsNone(await self.store.brand_col_for("нет-такого"))


class MirrorTest(unittest.IsolatedAsyncioTestCase):
    """Снимок для формы брендов: строка на бренд, как у листов."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = SupplierStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        supplier = await self.store.add_supplier("Керамика")
        await self.store.add_signature(supplier.id, "hash-1", sample_name="Остатки.xls")
        await self.store.remember_marks("hash-1", [("ABK", 22, "000000265", "ABK"),
                                                   ("Dogma", 0, "", "")])
        await self.store.set_marks_by_signature(
            "hash-1", [{"brand": "ABK", "parse": True, "tm_code": "000000265",
                        "tm_name": "ABK", "discount": 17.5}])

    async def asyncTearDown(self):
        self._dir.cleanup()

    def provider(self):
        from src.onec.model_provider import OnecProvider

        class Service:
            prices = []

            def lock_of(self, _id):
                return None

        return OnecProvider(onec=None, service=Service(), suppliers=self.store)

    async def test_row_per_brand_with_flag_mark_and_discount(self):
        rows = await self.provider().marks_snapshot()
        self.assertEqual([(r["brand"], r["parse"], r["tm_code"], r["discount"])
                          for r in rows],
                         [("ABK", True, "000000265", 17.5), ("Dogma", False, "", None)])

    async def test_vanished_brand_is_shown_with_zero_rows(self):
        """Скрыв его, потеряли бы и новость «бренда больше нет», и возможность снять
        флажок."""
        rows = await self.provider().marks_snapshot()
        self.assertEqual(next(r["rows"] for r in rows if r["brand"] == "Dogma"), 0)

    async def test_supplier_goes_by_code(self):
        rows = await self.provider().marks_snapshot()
        self.assertTrue(all(r["supplier_code"] for r in rows))

    async def test_format_without_brands_is_not_shown(self):
        supplier = await self.store.add_supplier("Другой")
        await self.store.add_signature(supplier.id, "hash-2")
        rows = await self.provider().marks_snapshot()
        self.assertEqual({r["signature"] for r in rows}, {"hash-1"})

    async def test_without_a_catalogue_it_is_empty(self):
        from src.onec.model_provider import OnecProvider

        class Service:
            prices = []

            def lock_of(self, _id):
                return None

        provider = OnecProvider(onec=None, service=Service())
        self.assertEqual(await provider.marks_snapshot(), [])


class CommandTest(unittest.IsolatedAsyncioTestCase):
    """Команда из формы 1С: флажки, марки, скидки и курс прайса."""

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
        supplier = await self.store.add_supplier("Керамика")
        await self.store.add_signature(supplier.id, "hash-1")
        await self.store.remember_marks("hash-1", [("ABK", 22, "", ""),
                                                   ("VitrA", 202, "", "")])

        self.model = PriceListService(
            self.model_store, self.store,
            save_file=lambda content, name: price_files.save(db, name, content))
        await self.model.load()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def send(self, payload, price_id=None):
        await self.model.apply(Command(kind=CommandKind.SET_SIGNATURE_MARKS,
                                       source="1c", actor="1c:Саша",
                                       price_id=price_id, payload=payload))

    async def test_flags_marks_and_discounts_are_stored(self):
        await self.send({"signature": "hash-1", "marks": [
            {"brand": "ABK", "parse": True, "tm_code": "000000265",
             "tm_name": "ABK", "discount": 17.5},
            {"brand": "VitrA", "parse": False}]})

        rows = {m.brand: m for m in await self.store.marks_for("hash-1")}
        self.assertTrue(rows["ABK"].parse)
        self.assertEqual(rows["ABK"].tm_code, "000000265")
        self.assertEqual(rows["ABK"].discount, 17.5)
        self.assertFalse(rows["VitrA"].parse)

    async def test_currency_and_rate_travel_together(self):
        """Курс без валюты выглядит заданным, а чего он курс — неизвестно: держать их
        врозь нельзя. Код валюты — по международному классификатору."""
        await self.send({"signature": "hash-1", "marks": [],
                         "currency": "978", "currency_name": "EUR", "rate": 98.5})
        self.assertEqual(await self.store.last_currency("hash-1"),
                         {"code": "978", "name": "EUR", "rate": 98.5})

    async def test_unknown_signature_is_refused(self):
        await self.send({"signature": "нет-такого", "marks": [{"brand": "ABK"}]})
        self.assertEqual(await self.store.marks_wanted("hash-1"), [])

    async def test_without_marks_it_is_refused(self):
        await self.send({"signature": "hash-1"})
        self.assertEqual(await self.store.marks_wanted("hash-1"), [])


class FormatCommandTest(unittest.IsolatedAsyncioTestCase):
    """ОДНА команда на оба списка — единая форма формата (решение админа 03.10.2026).

    Двумя командами это не сделать: адрес у них одинаковый (прайс и задача 0), и защита от
    второго нажатия в 1С отклонила бы вторую с «по этому прайсу уже отправлена команда» —
    половина выбора молча не доехала бы.
    """

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
        supplier = await self.store.add_supplier("Керамика")
        await self.store.add_signature(supplier.id, "hash-1",
                                       sheet_list="TDSheet, АКЦИИ")
        await self.store.remember_marks("hash-1", [("ABK", 22, "", ""),
                                                   ("VitrA", 202, "", "")])
        self.model = PriceListService(
            self.model_store, self.store,
            save_file=lambda content, name: price_files.save(db, name, content))
        await self.model.load()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def send(self, payload):
        await self.model.apply(Command(kind=CommandKind.SET_SIGNATURE_FORMAT,
                                       source="1c", actor="1c:Саша", payload=payload))

    async def test_both_lists_are_saved_at_once(self):
        await self.send({"signature": "hash-1",
                         "sheets": ["TDSheet"],
                         "marks": [{"brand": "ABK", "parse": True, "discount": 17.5},
                                   {"brand": "VitrA", "parse": False}],
                         "currency": "978", "currency_name": "EUR", "rate": 98.4})

        self.assertEqual(await self.store.sheets_for("hash-1"), "TDSheet")
        self.assertEqual(await self.store.marks_wanted("hash-1"), ["ABK"])
        self.assertEqual(await self.store.last_currency("hash-1"),
                         {"code": "978", "name": "EUR", "rate": 98.4})

    async def test_missing_key_does_not_wipe_the_choice(self):
        """Отсутствующий ключ — «про это ничего не сказано», пустой список — «ничего не
        отмечено». Путать нельзя: обрезанная команда стёрла бы выбор админа."""
        await self.send({"signature": "hash-1", "sheets": ["TDSheet"],
                         "marks": [{"brand": "ABK", "parse": True}]})

        await self.send({"signature": "hash-1", "marks": [{"brand": "ABK",
                                                           "parse": False}]})

        self.assertEqual(await self.store.sheets_for("hash-1"), "TDSheet")
        self.assertEqual(await self.store.marks_wanted("hash-1"), [])

    async def test_empty_lists_are_a_legal_choice(self):
        """«Не разбирать этот формат» — законное решение, а не промах."""
        await self.send({"signature": "hash-1", "sheets": [], "marks": []})
        self.assertEqual(await self.store.sheets_for("hash-1"), "")
        self.assertEqual(await self.store.marks_wanted("hash-1"), [])

    async def test_unknown_signature_is_refused(self):
        await self.send({"signature": "нет-такого", "sheets": ["TDSheet"]})
        self.assertEqual(await self.store.sheets_for("hash-1"), "")

    async def test_command_without_lists_is_refused(self):
        """Ни листов, ни брендов — команде нечего применять, и она отклоняется: курс без
        выбора ехал бы отдельной полуправдой."""
        await self.send({"signature": "hash-1", "rate": 98})
        got = await self.store.last_currency("hash-1")
        self.assertIsNone(got["rate"])

    async def test_it_does_not_collapse_in_the_queue(self):
        """Номер прайса у неё есть, но объект — СИГНАТУРА: схлопнувшись с «сменить статус
        прайса», она потеряла бы одно из двух несвязанных решений."""
        command = Command(kind=CommandKind.SET_SIGNATURE_FORMAT, price_id=6,
                          payload={"signature": "hash-1"})
        self.assertIsNone(command.coalesce_key())


class TelegramTest(unittest.IsolatedAsyncioTestCase):
    """Команда `/signature_marks` — отмечать можно и до выкладки формы в 1С."""

    class Msg:
        def __init__(self):
            self.last = ""

        async def answer(self, text, **kw):
            self.last = text

    class Args:
        def __init__(self, args):
            self.args = args

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = SupplierStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        supplier = await self.store.add_supplier("Керамика")
        await self.store.add_signature(supplier.id, "hash-1", sample_name="Остатки.xls")
        await self.store.remember_marks("hash-1", [("ABK", 22, "000000265", "ABK"),
                                                   ("VitrA", 202, "", ""),
                                                   ("Dogma", 19, "", "")])
        self.msg = self.Msg()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_listing_shows_rows_and_missing_marks(self):
        await ch.cmd_signature_marks(self.msg, self.Args("1"), self.store, True)
        self.assertIn("ABK — 22 поз. → ABK", self.msg.last)
        self.assertIn("марки в 1С нет", self.msg.last)

    async def test_numbers_tick_the_brands(self):
        await ch.cmd_signature_marks(self.msg, self.Args("1 1,3"), self.store, True)
        self.assertEqual(await self.store.marks_wanted("hash-1"), ["ABK", "Dogma"])

    async def test_dash_clears_everything(self):
        await ch.cmd_signature_marks(self.msg, self.Args("1 1,2"), self.store, True)
        await ch.cmd_signature_marks(self.msg, self.Args("1 -"), self.store, True)
        self.assertEqual(await self.store.marks_wanted("hash-1"), [])

    async def test_format_without_brands_says_so(self):
        supplier = await self.store.add_supplier("Другой")
        await self.store.add_signature(supplier.id, "hash-2")
        await ch.cmd_signature_marks(self.msg, self.Args("2"), self.store, True)
        self.assertIn("брендов не видели", self.msg.last)

    async def test_manager_cannot(self):
        await ch.cmd_signature_marks(self.msg, self.Args("1 1"), self.store, False)
        self.assertEqual(await self.store.marks_wanted("hash-1"), [])


class ReportTest(unittest.TestCase):
    """Отчёт после разбора: третья группа обязательна и по брендам тоже."""

    def tools(self, **kw):
        from src.model.task_builder import TaskBuilderTools

        t = TaskBuilderTools(b"x", "Остатки.xls")
        for name, value in kw.items():
            setattr(t, name, value)
        return t

    def test_parsed_and_hidden_brands_are_both_named(self):
        from src.model.task_builder import sheets_report

        got = sheets_report(self.tools(
            read_sheets=["TDSheet"], parsed_sheets=["TDSheet"],
            parsed_marks=["ABK", "VitrA"], skipped_marks=["Dogma", "WOW"],
            kept_rows=224, whole_rows=1280))
        self.assertIn("Разобраны бренды (224 строк из 1280): ABK, VitrA", got)
        self.assertIn("Бренды не отмечены и в разбор не входили: Dogma, WOW", got)

    def test_nothing_ticked_tells_where_to_tick(self):
        from src.model.task_builder import sheets_report

        got = sheets_report(self.tools(pick_problem="бренды не отмечены"))
        self.assertIn("Бренды не отмечены", got)
        self.assertIn("«Бренды» в форме 1С", got)

    def test_empty_intersection_is_not_sold_as_no_work(self):
        from src.model.task_builder import sheets_report

        got = sheets_report(self.tools(
            pick_problem="ни одного отмеченного бренда нет в отмеченных листах",
            skipped_marks=["ABK", "VitrA"]))
        self.assertIn("Пересечение пусто", got)
        self.assertIn("ABK", got)


if __name__ == "__main__":
    unittest.main()
