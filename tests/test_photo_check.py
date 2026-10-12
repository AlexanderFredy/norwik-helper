"""«Покажи, где не добавлены фото» (решение админа 30.09.2026).

Вопрос менеджера к агенту, а не инвентаризация каталога: смотрятся только товары,
заведённые за последние месяцы, и только выгружаемые на сайт. Сам список уходит мимо
модели — ей возвращаются числа.
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from src.agent.tools import ToolExecutor, _months_ago  # noqa: F401
from src.onec.client import NomItem, Nomenclature, TradeMark
from src.website_tool import photo_report, photos


def item(ref="T1", site_id="1001", created="2026-09-01", name="Ламинат Classen Дуб Авола",
         parent="Adventure WR 1290x193x8", size="1290x193x8", collection="",
         not_exported=False, tm="Classen / Классен", type_ref="000000003"):
    return NomItem(
        ref=ref, id=site_id, name=name, article="", unit="м2", size=size,
        product_type="Ламинат", collection=collection, parent=parent,
        collection_ref="F1", alt_units={}, purchase=None, retail=None, rrc=None,
        site_name=name, not_exported=not_exported, created=created, tm=tm,
        tm_code="1", product_type_ref=type_ref)


class MonthsAgoTest(unittest.TestCase):
    """Считаем по календарю: «месяц = 30 дней» ошибается на трое суток, а за сутки
    заводят товары."""

    def test_plain(self):
        self.assertEqual(_months_ago(3, date(2026, 9, 30)), "2026-06-30")

    def test_crosses_the_year(self):
        self.assertEqual(_months_ago(3, date(2026, 2, 15)), "2025-11-15")

    def test_missing_day_moves_to_the_first(self):
        """31 мая минус 3 месяца — 31 февраля, которого нет: граница обязана существовать
        в календаре."""
        self.assertEqual(_months_ago(3, date(2026, 5, 31)), "2026-03-01")

    def test_december_rolls_over(self):
        self.assertEqual(_months_ago(1, date(2026, 1, 31)), "2025-12-31")


class FakeHTTP:
    """Отвечает кодами по пути запроса — так и выглядит сайт для проверки."""

    def __init__(self, codes):
        self.codes = codes
        self.asked: list[str] = []

    def head(self, path):
        self.asked.append(path)
        if path not in self.codes:
            raise AssertionError(f"неожиданный запрос: {path}")
        code = self.codes[path]
        if isinstance(code, Exception):
            raise code
        return type("R", (), {"status_code": code})()


def checker(codes):
    obj = photos.PhotoChecker.__new__(photos.PhotoChecker)
    obj._client = FakeHTTP(codes)
    obj._workers = 2
    return obj


class CheckerTest(unittest.TestCase):

    def test_folder_403_means_photo(self):
        """403 — папка есть, листинг закрыт. Сверено на 25 живых карточках."""
        c = checker({"/images/products/1001/": 403})
        self.assertEqual(c.status("1001"), photos.HAS)
        self.assertEqual(len(c._client.asked), 1, "страницу не трогаем вовсе")

    def test_no_folder_but_card_means_no_photo(self):
        c = checker({"/images/products/1001/": 404, "/item/1001": 200})
        self.assertEqual(c.status("1001"), photos.NONE)

    def test_no_folder_no_card(self):
        """Товар ещё не выгрузился на сайт — это не «нет фото»."""
        c = checker({"/images/products/1001/": 404, "/item/1001": 404})
        self.assertEqual(c.status("1001"), photos.NO_CARD)

    def test_empty_id_is_not_a_request(self):
        c = checker({})
        self.assertEqual(c.status(""), photos.NO_CARD)

    def test_site_failure_is_not_absence(self):
        """Придуманная работа хуже ненайденной: сбой сети не выдаём за отсутствие фото."""
        c = checker({"/images/products/1001/": RuntimeError("оборвалось")})
        self.assertEqual(c.status("1001"), photos.FAILED)

    def test_batch_asks_each_id_once(self):
        c = checker({"/images/products/1001/": 403, "/images/products/1002/": 403})
        out = c.statuses(["1001", "1002", "1001", " 1002 "])
        self.assertEqual(out, {"1001": photos.HAS, "1002": photos.HAS})
        self.assertEqual(len(c._client.asked), 2)


def row(tm="Classen", collection="Adventure WR", name="Дуб Авола", site_id="1001"):
    return photo_report.Row(tm=tm, collection=collection, name=name,
                            url=photos.item_url(site_id), created="2026-09-01")


class ReportTest(unittest.TestCase):

    def test_grouped_by_mark_and_collection(self):
        text = photo_report.render([row(name="Дуб Авола"), row(name="Дуб Брен")],
                                   since="2026-06-30", checked=9, marks=2)
        self.assertEqual(text.count("Classen / Adventure WR"), 1, "заголовок группы один")
        self.assertIn("— Дуб Авола — https://www.norwik.ru/item/1001", text)

    def test_link_is_the_site_card(self):
        self.assertEqual(photos.item_url("189490"), "https://www.norwik.ru/item/189490")

    def test_nothing_missing_is_said_plainly(self):
        text = photo_report.render([], since="2026-06-30", checked=12, marks=3)
        self.assertIn("Все новые товары с фото", text)
        self.assertIn("12 новых позиций", text)

    def test_not_exported_are_counted_but_not_listed(self):
        """Показывать их админ просил не списком — но молчать о них нельзя: «без фото 3»
        при сорока не доехавших читается как «почти всё в порядке»."""
        text = photo_report.render([row()], since="2026-06-30", checked=44, marks=1,
                                   no_card=40)
        self.assertIn("40 поз. на сайт пока не выгрузились", text)
        self.assertEqual(text.count("https://"), 1, "их ссылок в списке нет")

    def test_site_failures_are_named(self):
        text = photo_report.render([], since="2026-06-30", checked=5, marks=1, failed=2)
        self.assertIn("2 поз. проверить не удалось", text)

    def test_two_posts_is_the_border(self):
        self.assertTrue(photo_report.fits_chat("x" * 8192))
        self.assertFalse(photo_report.fits_chat("x" * 8193))

    def test_excel_has_headers_and_rows(self):
        from openpyxl import load_workbook

        with tempfile.TemporaryDirectory() as tmp:
            path = photo_report.to_excel([row(name="Дуб Авола")],
                                         Path(tmp) / "нет фото.xlsx")
            sheet = load_workbook(path).active
            self.assertEqual([c.value for c in sheet[1]][:4],
                             ["Марка", "Коллекция", "Наименование", "Ссылка"])
            self.assertEqual(sheet.cell(row=2, column=3).value, "Дуб Авола")
            self.assertEqual(sheet.cell(row=2, column=4).hyperlink.target,
                             "https://www.norwik.ru/item/1001")


class FakeOnec:
    """`None` в качестве кода марки — запрос БЕЗ `tm`, то есть по всем выгружаемым."""

    def __init__(self, dumps, marks=None, errors=0, old_bsl=False):
        self._dumps, self._errors, self._old = dumps, errors, old_bsl
        self.marks = marks or [TradeMark(name="Classen / Классен", code="1")]
        self.asked: list[tuple] = []

    def selling_tm(self, all_marks=False):
        return list(self.marks)

    def by_tm_all(self, code, created_from=None, **kw):
        self.asked.append((code, created_from))
        if self._old and code is None:
            # Старый by-tm.bsl: запрос без марки не принимает и отвечает пустым списком.
            return Nomenclature(tm="", total=0, items=[],
                                errors=[{"ref": "", "code": "tm_missing",
                                         "message": "Не передан параметр tm"}])
        items = self._dumps.get(code, [])
        return Nomenclature(tm="ТМ", total=len(items), items=list(items),
                            errors=[{"ref": "X"}] * self._errors)


class FakeChecker:
    def __init__(self, verdicts):
        self.verdicts = verdicts

    def __call__(self, *_a, **_kw):
        return self

    def statuses(self, ids):
        return {(i or "").strip(): self.verdicts.get(i, photos.HAS) for i in ids}

    def close(self):
        pass


def _days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def _text(outputs) -> str:
    return "\n".join(o for o in outputs if isinstance(o, str))


class FakeSettings:
    def __init__(self, values=None):
        self.values = dict(values or {})

    async def get_setting(self, key, default=""):
        return self.values.get(key, default)

    async def set_setting(self, key, value):
        self.values[key] = value


class ToolTest(unittest.IsolatedAsyncioTestCase):
    """Модель задаёт вопрос кнопками, проверку запускает нажатие (решение админа 12.10.2026)."""

    def executor(self, onec, settings=None):
        return ToolExecutor(mail=None, norwik=None, onec=onec, pricing_store=settings)

    async def ask(self, ex, inp=None):
        """Вопрос модели → (ответ модели, номер запроса, кнопки)."""
        answer = await ex._photos_missing(dict(inp or {}))
        buttons = ex.take_buttons()
        token = buttons[0][0][1].split(":")[1] if buttons else None
        return answer, token, buttons

    async def run_tool(self, onec, verdicts, inp=None, recent=False):
        """По умолчанию — «любые товары» в ламинате."""
        ex = self.executor(onec)
        inp = {"product_type": "ламинат", **(inp or {})}
        with patch.object(photos, "PhotoChecker", FakeChecker(verdicts)):
            _, token, _ = await self.ask(ex, inp)
            ex.take_pending()
            outputs = await ex.photo_check(ex.photo_request(token), recent=recent)
        return ex, outputs

    # ---------------------------------------------------------------- вопрос

    async def test_the_question_is_always_asked_and_nothing_is_walked(self):
        """«Любые или недавние» спрашивается ВСЕГДА, и до ответа в 1С не ходим."""
        onec = FakeOnec({None: [item()]})
        ex = self.executor(onec)
        answer, token, buttons = await self.ask(ex, {"product_type": "ламинат"})
        self.assertEqual(onec.asked, [])
        self.assertIn("Выберите вариант кнопкой ниже: любые товары или только недавно "
                      "добавленные.", answer)
        self.assertNotIn("список брендов", answer)
        question, _ = ex.take_pending()
        self.assertIn("любые товары или вас интересуют только недавно добавленные", question)
        self.assertIn("(глубина поиска 180 дн)", question)
        self.assertIn("«Ламинат»", question)
        self.assertEqual([row[0][1] for row in buttons],
                         [f"photo:{token}:any", f"photo:{token}:new"])

    async def test_no_brand_hint_when_brands_named(self):
        answer, _, _ = await self.ask(self.executor(FakeOnec({})), {"tms": ["Classen"]})
        self.assertIn("любые товары или только недавно добавленные", answer)
        self.assertNotIn("список брендов", answer)

    async def test_depth_comes_from_the_setting(self):
        ex = self.executor(FakeOnec({}), FakeSettings({"photo_recent_days": "90"}))
        _, _, buttons = await self.ask(ex, {})
        self.assertIn("90 дн", buttons[1][0][0])
        self.assertIn("глубина поиска 90 дн", ex.take_pending()[0])

    async def test_buttons_fit_telegram_callback_limit(self):
        _, _, buttons = await self.ask(self.executor(FakeOnec({})), {})
        for row in buttons:
            for _, data in row:
                self.assertLessEqual(len(data.encode()) + 10, 64, "и место под код вида")

    async def test_unknown_category_is_named_before_the_question(self):
        onec = FakeOnec({})
        ex = self.executor(onec)
        answer, token, _ = await self.ask(ex, {"product_type": "космолёты"})
        self.assertIn("«космолёты»", answer)
        self.assertIsNone(token)

    async def test_request_is_forgotten_once_run(self):
        """Второе нажатие не запускает минутный обход второй раз."""
        ex = self.executor(FakeOnec({}))
        _, token, _ = await self.ask(ex, {})
        ex.forget_photo_request(token)
        self.assertIsNone(ex.photo_request(token))

    # ------------------------------------------------------- «любые товары»

    async def test_any_without_category_is_refused_by_code(self):
        """Анализ всей товарной массы запрещён — и проверяет это КОД, а не промпт."""
        onec = FakeOnec({None: [item()]})
        ex = self.executor(onec)
        outputs = await ex.photo_check({"type_code": "", "tms": []}, recent=False)
        self.assertIn("категорию", _text(outputs))
        self.assertEqual(onec.asked, [], "в 1С не ходили")

    async def test_any_checks_the_whole_category(self):
        """Любые товары — весь ассортимент категории на сайте, старые позиции тоже."""
        onec = FakeOnec({None: [item(site_id="1001", created=""),
                                item(site_id="1002", created="2020-01-01")]})
        _, outputs = await self.run_tool(onec, {"1001": photos.NONE, "1002": photos.NONE})
        self.assertEqual(onec.asked, [(None, None)], "окна новизны нет")
        text = _text(outputs)
        self.assertIn("item/1001", text)
        self.assertIn("item/1002", text)
        self.assertIn("категории «Ламинат»", text)

    async def test_list_goes_to_the_manager(self):
        onec = FakeOnec({None: [item(site_id="1001"), item(site_id="1002")]})
        _, outputs = await self.run_tool(onec, {"1001": photos.NONE})
        self.assertIn("https://www.norwik.ru/item/1001", _text(outputs))
        self.assertNotIn("item/1002", _text(outputs))

    async def test_discontinued_are_skipped(self):
        onec = FakeOnec({None: [item(site_id="1001", not_exported=True)]})
        _, outputs = await self.run_tool(onec, {"1001": photos.NONE})
        self.assertIn("нет — проверять нечего", _text(outputs))

    async def test_items_without_a_card_are_not_listed(self):
        onec = FakeOnec({None: [item(site_id="1001"), item(site_id="1002")]})
        _, outputs = await self.run_tool(onec, {"1001": photos.NO_CARD, "1002": photos.NONE})
        text = _text(outputs)
        self.assertNotIn("item/1001", text)
        self.assertIn("1 поз. на сайт пока не выгрузились", text)

    async def test_long_list_becomes_excel(self):
        rows = [item(ref=f"R{i}", site_id=str(2000 + i),
                     name=f"Ламинат Classen Очень Длинное Название Декора Номер {i}")
                for i in range(200)]
        onec = FakeOnec({None: rows})
        _, outputs = await self.run_tool(onec, {str(2000 + i): photos.NONE for i in range(200)})
        files = [o for o in outputs if isinstance(o, Path)]
        self.assertEqual(len(files), 1)
        self.assertTrue(str(files[0]).endswith(".xlsx"))
        self.assertTrue(files[0].name.startswith("Без фото Ламинат "), files[0].name)
        self.assertIn("файлом Excel", _text(outputs))
        self.assertLess(len(_text(outputs)), 500, "сам список в чат не шлём")
        files[0].unlink(missing_ok=True)

    async def test_one_request_not_one_per_mark(self):
        """Обход по марке стоил 285 с на 166 марок (замер 30.09.2026)."""
        onec = FakeOnec({None: [item()]},
                        marks=[TradeMark(name=f"Марка {i}", code=str(i)) for i in range(166)])
        await self.run_tool(onec, {"1001": photos.HAS})
        self.assertEqual(onec.asked, [(None, None)])

    async def test_mark_name_comes_from_the_item(self):
        onec = FakeOnec({None: [item(site_id="1001", tm="Peli")]})
        _, outputs = await self.run_tool(onec, {"1001": photos.NONE})
        self.assertIn("Peli / Adventure WR", _text(outputs))

    async def test_old_bsl_falls_back_to_walking_marks(self):
        """Старый by-tm.bsl без марки отвечает tm_missing — «товаров нет» это не значит."""
        onec = FakeOnec({"1": [item(site_id="1001")]}, old_bsl=True)
        _, outputs = await self.run_tool(onec, {"1001": photos.NONE})
        self.assertEqual([code for code, _ in onec.asked], [None, "1"])
        self.assertIn("item/1001", _text(outputs))
        self.assertIn("by-tm.bsl", _text(outputs))

    async def test_mark_filter(self):
        onec = FakeOnec({"1": [item()]},
                        marks=[TradeMark(name="Classen / Классен", code="1"),
                               TradeMark(name="Peli", code="2")])
        await self.run_tool(onec, {"1001": photos.HAS}, {"tm": "classen"})
        self.assertEqual([code for code, _ in onec.asked], ["1"])

    async def test_unknown_mark(self):
        _, outputs = await self.run_tool(FakeOnec({}), {}, {"tm": "нетакой"})
        self.assertIn("нет марок: «нетакой»", _text(outputs))

    async def test_lost_positions_are_named(self):
        onec = FakeOnec({None: [item()]}, errors=3)
        _, outputs = await self.run_tool(onec, {"1001": photos.NONE})
        self.assertIn("не отдала 3", _text(outputs))

    async def test_other_categories_are_dropped(self):
        """Старый BSL фильтр вида мог не применить — отбор повторяем у себя."""
        onec = FakeOnec({None: [item(site_id="1001"),
                                item(site_id="1002", type_ref="999999999")]})
        _, outputs = await self.run_tool(onec, {"1001": photos.NONE, "1002": photos.NONE})
        self.assertIn("item/1001", _text(outputs))
        self.assertNotIn("item/1002", _text(outputs))

    async def test_items_without_site_id_are_not_checked(self):
        onec = FakeOnec({None: [item(site_id=""), item(site_id="1002")]})
        _, outputs = await self.run_tool(onec, {"1002": photos.NONE})
        self.assertIn("1 позиций", _text(outputs))

    async def test_marks_list_and_unknown_mark_named(self):
        onec = FakeOnec({"1": [item(site_id="1001")],
                         "2": [item(site_id="1002", tm="Peli")]},
                        marks=[TradeMark(name="Classen / Классен", code="1"),
                               TradeMark(name="Peli", code="2"),
                               TradeMark(name="Kronotex", code="3")])
        _, outputs = await self.run_tool(onec, {"1001": photos.NONE, "1002": photos.NONE},
                                         {"tms": ["Классен", "peli", "нетакой"]})
        self.assertEqual(sorted(code for code, _ in onec.asked), ["1", "2"])
        self.assertIn("Без фото на сайте: 2 поз.", _text(outputs))
        self.assertIn("«нетакой»", _text(outputs))

    # ------------------------------------------- бренды на шаге категории

    def marks3(self):
        return [TradeMark(name="Classen / Классен", code="1"),
                TradeMark(name="Peli", code="2"),
                TradeMark(name="Kronotex", code="3")]

    async def test_brands_text_is_remembered(self):
        onec = FakeOnec({}, marks=self.marks3())
        ex = self.executor(onec)
        _, token, _ = await self.ask(ex, {})
        picked, unknown = await ex.set_photo_brands(token, "Классен, peli и нетакой")
        self.assertEqual(picked, ["Classen / Классен", "Peli"])
        self.assertEqual(unknown, ["нетакой"])
        self.assertEqual(ex.photo_request(token)["tms"], ["Classen / Классен", "Peli"])

    async def test_a_question_is_not_taken_for_brands(self):
        """Обычный вопрос на этом шаге уходит агенту, а не съедается как «бренды»."""
        ex = self.executor(FakeOnec({}, marks=self.marks3()))
        _, token, _ = await self.ask(ex, {})
        for text in ("а сколько стоит Classen Adventure у поставщиков сейчас", "да", "ок"):
            picked, _ = await ex.set_photo_brands(token, text)
            self.assertEqual(picked, [], text)
        self.assertEqual(ex.photo_request(token)["tms"], [])

    async def test_category_and_brands_intersect(self):
        """Выдача — пересечение категории и брендов (решение админа 12.10.2026)."""
        onec = FakeOnec({"1": [item(site_id="1001"),
                               item(site_id="1003", type_ref="999999999")],
                         "2": [item(site_id="1002", tm="Peli")],
                         "3": [item(site_id="1004", tm="Kronotex")]},
                        marks=self.marks3())
        ex = self.executor(onec)
        _, token, _ = await self.ask(ex, {})
        await ex.set_photo_brands(token, "Classen, Peli")
        request = ex.photo_request(token)
        request.update(type_code="000000003", type_name="Ламинат")
        verdicts = {i: photos.NONE for i in ("1001", "1002", "1003", "1004")}
        with patch.object(photos, "PhotoChecker", FakeChecker(verdicts)):
            outputs = await ex.photo_check(request, recent=False)
        text = _text(outputs)
        self.assertIn("item/1001", text)
        self.assertIn("item/1002", text)
        self.assertNotIn("item/1003", text, "другая категория")
        self.assertNotIn("item/1004", text, "другой бренд")
        self.assertEqual(sorted(c for c, _ in onec.asked), ["1", "2"])

    async def test_brand_wait_ends_with_the_request(self):
        ex = self.executor(FakeOnec({}))
        _, token, _ = await self.ask(ex, {})
        ex.await_photo_brands(10, 20, token, 555)
        self.assertEqual(ex.photo_brand_wait(10, 20), (token, 555))
        self.assertIsNone(ex.photo_brand_wait(10, 21), "другой человек")
        ex.forget_photo_request(token)
        self.assertIsNone(ex.photo_brand_wait(10, 20), "категорию нажали — ждать нечего")

    # ------------------------------------------------- «недавно добавленные»

    async def test_recent_goes_to_1c_with_the_depth(self):
        """Отбор на стороне 1С: иначе тянули бы каталог, чтобы отбросить почти всё."""
        onec = FakeOnec({None: [item()]})
        await self.run_tool(onec, {}, recent=True)
        self.assertEqual(onec.asked, [(None, _days_ago(180))])

    async def test_recent_needs_no_category(self):
        """Недавно добавленные ограничены сроком — категория тут по желанию."""
        onec = FakeOnec({None: [item(site_id="1001", created=_days_ago(10)),
                                item(site_id="1002", created=_days_ago(10),
                                     type_ref="999999999")]})
        _, outputs = await self.run_tool(onec, {"1001": photos.NONE, "1002": photos.NONE},
                                         {"product_type": None}, recent=True)
        text = _text(outputs)
        self.assertIn("item/1001", text)
        self.assertIn("item/1002", text, "все категории")
        self.assertIn("новых позиций", text)

    async def test_recent_drops_old_items_even_if_1c_sent_them(self):
        """Старый by-tm.bsl параметр не заметит и отдаст всё — отбор повторяем у себя."""
        onec = FakeOnec({None: [item(site_id="1001", created="2020-01-01"),
                                item(site_id="1002", created=_days_ago(30))]})
        _, outputs = await self.run_tool(onec, {"1001": photos.NONE, "1002": photos.NONE},
                                         recent=True)
        self.assertIn("item/1002", _text(outputs))
        self.assertNotIn("item/1001", _text(outputs))

    async def test_recent_depth_setting_reaches_1c(self):
        onec = FakeOnec({None: [item()]})
        ex = self.executor(onec, FakeSettings({"photo_recent_days": "30"}))
        with patch.object(photos, "PhotoChecker", FakeChecker({})):
            await ex.photo_check({"type_code": "", "tms": []}, recent=True)
        self.assertEqual(onec.asked, [(None, _days_ago(30))])

    async def test_brief_answers_from_the_journal_without_a_walk(self):
        """Полный отчёт идёт в 1С и на сайт около минуты; сводка уже посчитана ежедневной
        проверкой, и ходить за ней никуда не надо."""
        from src.storage.photo_watch import PhotoWatchStore

        with tempfile.TemporaryDirectory() as tmp:
            watch = PhotoWatchStore(Path(tmp) / "w.db")
            await watch.init()
            await watch.observe([{"ref": "A", "site_id": "1", "tm": "Classen",
                                  "collection": "Manor", "name": "Вернон",
                                  "created": "2026-08-01", "state": photos.NONE}],
                                today="2026-09-28")
            onec = FakeOnec({})
            ex = ToolExecutor(mail=None, norwik=None, onec=onec, photo_watch=watch)
            answer = await ex._photos_missing({"brief": True})

            self.assertEqual(onec.asked, [], "в 1С не ходили вовсе")
            self.assertIn("сводка", answer.lower())
            text, _ = ex.take_pending()
            self.assertIn("Всего ждут фото: 1 поз.", text)
            self.assertIn("- Classen — 1", text)
            # Журнал наполняется раз в сутки: вчерашнюю картину нельзя выдавать за свежую.
            self.assertIn("По данным проверки от 28.09.2026", text)

    def test_todays_data_needs_no_age_line(self):
        """Приписка про возраст нужна, только когда он не сегодняшний, — иначе она шум."""
        from datetime import date

        rows = [photo_report.Row(tm="Classen", collection="Manor", name="Вернон",
                                 url="u", created="2026-08-01")]

        class W:
            def __init__(self, row):
                self.tm, self.created = row.tm, row.created

            def waiting_days(self, today=None):
                return 60

        stamp = date.today().isoformat()
        text = photo_report.digest(None, [W(rows[0])], as_of=stamp)
        self.assertNotIn("По данным проверки", text)

    async def test_brief_on_an_empty_journal_says_so(self):
        """«Ждут 0» при ненаполненном журнале читалось бы как «всё в порядке»."""
        from src.storage.photo_watch import PhotoWatchStore

        with tempfile.TemporaryDirectory() as tmp:
            watch = PhotoWatchStore(Path(tmp) / "w.db")
            await watch.init()
            ex = ToolExecutor(mail=None, norwik=None, onec=FakeOnec({}),
                              photo_watch=watch)
            answer = await ex._photos_missing({"brief": True})
            self.assertIn("Журнал пуст", answer)

    async def test_brief_without_a_journal(self):
        ex = ToolExecutor(mail=None, norwik=None, onec=FakeOnec({}))
        self.assertIn("не подключён", await ex._photos_missing({"brief": True}))

    async def test_without_1c(self):
        ex = self.executor(None)
        self.assertIn("не настроена", await ex._photos_missing({}))


if __name__ == "__main__":
    unittest.main()
