"""Отложенные задачи (§9.7): кнопки, хранение прайса, устаревание, команды.

Список переживает конец прогона и перезапуск бота, поэтому лежит отдельно от `price_run`.
Прайс сохраняется на сервере, чтобы возврат не требовал пересылки 12-мегабайтного файла.

Устаревание считается ТОЛЬКО по дате прайса: тот же прайс, присланный заново, — обычный
способ вернуться к отложенному, и ронять из-за него задачу нельзя.
"""
import tempfile
import unittest
from pathlib import Path

from src.agent.pricing_tools import PricingTools, clear_nomenclature_cache
from src.bot import pricing_handlers as ph
from src.storage import price_files
from src.storage.pricing import PricingStore
from tests.test_pricing_flow import FakeOnec, item

TMS = [{"code": "T1", "name": "Atlas Concorde Rus"}, {"code": "T2", "name": "Azteca"}]


def task(tm_code="T1", tm_name="Atlas Concorde Rus", collection="", **kw):
    return {"tm_code": tm_code, "tm_name": tm_name, "collection": collection,
            "collection_ref": "YO-A" if collection else "",
            "supplier": "Артисан-Проект", "price_doc": "Price.xls",
            "price_date": "2026-08-26", "signature": "sig1", **kw}


class DeferredStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_add_and_list(self):
        self.assertTrue(await self.store.defer_task(42, task()))
        self.assertTrue(await self.store.defer_task(42, task(collection="Allure")))
        rows = await self.store.list_deferred(42)
        self.assertEqual([ph._deferred_title(t) for t in rows],
                         ["Atlas Concorde Rus", "Atlas Concorde Rus / Allure"])

    async def test_repeat_is_ignored(self):
        await self.store.defer_task(42, task())
        self.assertFalse(await self.store.defer_task(42, task()))
        self.assertEqual(len(await self.store.list_deferred(42)), 1)

    async def test_needs_tm(self):
        self.assertFalse(await self.store.defer_task(42, task(tm_code="")))

    async def test_survives_run_and_dialog_reset(self):
        """Ради этого задачи и лежат отдельно от price_run."""
        await self.store.start_run(42, "Артисан", "Price.xls", TMS)
        await self.store.defer_task(42, task())
        await self.store.clear_run(42)
        await self.store.reset(42)
        self.assertEqual(len(await self.store.list_deferred(42)), 1)

    async def test_drop_after_success(self):
        await self.store.defer_task(42, task())
        await self.store.drop_deferred_for(42, "T1", "")
        self.assertEqual(await self.store.list_deferred(42), [])


class StaleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        await self.store.defer_task(42, task())

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def _stale(self):
        return [t for t in await self.store.list_deferred(42) if t["stale"]]

    async def test_newer_price_marks_stale(self):
        marked = await self.store.mark_stale(42, "Артисан-Проект", None, "2026-09-15")
        self.assertEqual(len(marked), 1)
        self.assertEqual(len(await self._stale()), 1)

    async def test_same_price_resent_is_not_stale(self):
        """Главное правило: повторная присылка того же прайса задачу не роняет."""
        self.assertEqual(await self.store.mark_stale(42, "Артисан-Проект", "sig1",
                                                     "2026-08-26"), [])
        self.assertEqual(await self._stale(), [])

    async def test_older_price_is_not_stale(self):
        self.assertEqual(await self.store.mark_stale(42, "Артисан-Проект", None,
                                                     "2026-07-01"), [])

    async def test_unknown_date_is_not_stale(self):
        self.assertEqual(await self.store.mark_stale(42, "Артисан-Проект", None, None), [])
        self.assertEqual(await self.store.mark_stale(42, "Артисан-Проект", None, "август"),
                         [])

    async def test_task_without_date_is_not_stale(self):
        await self.store.defer_task(42, task(tm_code="T9", price_date=""))
        marked = await self.store.mark_stale(42, "Артисан-Проект", None, "2026-09-15")
        self.assertNotIn("T9", [t["tm_code"] for t in marked])

    async def test_other_supplier_untouched(self):
        self.assertEqual(await self.store.mark_stale(42, "Монарх Логистик", None,
                                                     "2026-09-15"), [])

    async def test_supplier_matched_loosely(self):
        """«ООО Артисан-проект» и «Артисан-Проект» — один поставщик."""
        self.assertEqual(len(await self.store.mark_stale(42, "артисан-проект  ", None,
                                                         "2026-09-15")), 1)

    async def test_signature_also_matches(self):
        await self.store.defer_task(42, task(tm_code="T5", supplier="Другое имя"))
        marked = await self.store.mark_stale(42, "", "sig1", "2026-09-15")
        self.assertIn("T5", [t["tm_code"] for t in marked])


class FilesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Path(self._dir.name) / "t.db"
        self.store = PricingStore(self.db)
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_saved_once_and_removed_with_last_task(self):
        path = price_files.save(self.db, "Price.xls", b"price bytes")
        self.assertTrue(path.exists())
        self.assertEqual(price_files.save(self.db, "Price.xls", b"price bytes"), path)

        await self.store.defer_task(42, task(file_path=str(path)))
        await self.store.defer_task(42, task(tm_code="T2", tm_name="Azteca",
                                             file_path=str(path)))
        # первая задача ушла, но файл ещё нужен второй
        freed = await self.store.forget_deferred(42, (await self.store.list_deferred(42))[0]["id"])
        price_files.forget(freed)
        self.assertTrue(path.exists())

        freed = await self.store.clear_deferred(42)
        price_files.forget(freed)
        self.assertFalse(path.exists())

    async def test_load_round_trip_and_missing(self):
        path = price_files.save(self.db, "p.xlsx", b"data")
        self.assertEqual(price_files.load(path), b"data")
        self.assertIsNone(price_files.load(None))
        self.assertIsNone(price_files.load(self.db.parent / "prices" / "нет.xlsx"))

    async def test_empty_content_not_saved(self):
        self.assertIsNone(price_files.save(self.db, "p.xlsx", b""))

    async def test_clear_stale_only(self):
        await self.store.defer_task(42, task())
        await self.store.defer_task(42, task(tm_code="T2", tm_name="Azteca"))
        await self.store.mark_stale(42, "Артисан-Проект", None, "2026-09-15")
        await self.store.clear_deferred(42, stale_only=True)
        self.assertEqual(await self.store.list_deferred(42), [])


class FakeMessage:
    def __init__(self, user_id=42):
        self.from_user = type("U", (), {"id": user_id})()
        self.sent: list[str] = []

    async def answer(self, text, reply_markup=None, parse_mode=None):
        self.sent.append(text)
        return self

    async def edit_text(self, text, reply_markup=None):
        self.sent.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.sent)


class Args:
    def __init__(self, args): self.args = args


class CommandsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_empty_list_explains_buttons(self):
        msg = FakeMessage()
        await ph.cmd_deferred(msg, self.store, is_admin=True)
        self.assertIn("Отложить", msg.text)

    async def test_list_is_short(self):
        await self.store.defer_task(42, task())
        await self.store.defer_task(42, task(collection="Allure"))
        msg = FakeMessage()
        await ph.cmd_deferred(msg, self.store, is_admin=True)
        self.assertIn("1. Atlas Concorde Rus — прайс «Price.xls» от 2026-08-26", msg.text)
        self.assertIn("2. Atlas Concorde Rus / Allure", msg.text)

    async def test_stale_is_marked_in_list(self):
        await self.store.defer_task(42, task())
        await self.store.mark_stale(42, "Артисан-Проект", None, "2026-09-15")
        msg = FakeMessage()
        await ph.cmd_deferred(msg, self.store, is_admin=True)
        self.assertIn("устарело", msg.text)

    async def test_forget_by_number(self):
        await self.store.defer_task(42, task())
        msg = FakeMessage()
        await ph.cmd_deferred_forget(msg, Args("1"), self.store, is_admin=True)
        self.assertIn("Убрал из отложенных", msg.text)
        self.assertEqual(await self.store.list_deferred(42), [])

    async def test_bad_number(self):
        msg = FakeMessage()
        await ph.cmd_deferred_forget(msg, Args("7"), self.store, is_admin=True)
        self.assertIn("Укажите номер", msg.text)

    async def test_clear_all(self):
        await self.store.defer_task(42, task())
        msg = FakeMessage()
        await ph.cmd_deferred_clear(msg, self.store, is_admin=True)
        self.assertIn("очищен", msg.text)

    async def test_resume_without_file_says_so(self):
        await self.store.defer_task(42, task())
        msg = FakeMessage()
        await ph.cmd_deferred_resume(msg, Args("1"), None, object(), self.store,
                                     is_admin=True)
        self.assertIn("не найден", msg.text)

    async def test_manager_denied(self):
        msg = FakeMessage()
        await ph.cmd_deferred(msg, self.store, is_admin=False)
        self.assertIn("только администратору", msg.text)


class ButtonsTest(unittest.IsolatedAsyncioTestCase):
    """«Пропустить» очередь двигает, «Отложить» вдобавок заводит задачу."""

    async def asyncSetUp(self):
        clear_nomenclature_cache()
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.onec = FakeOnec([item("YO-1", 949, 1649, 1139)])
        await self.store.start_run(42, "Артисан", "Price.xls", TMS,
                                   price_date="2026-08-26")
        ph._files[42] = ("Price.xls", b"price bytes")
        tools = PricingTools(self.onec, self.store, user_id=42)
        tools.set_file("Price.xls", b"price bytes")
        await tools.execute("propose_prices", {"groups": [
            {"tm_code": "T1", "tm_name": "Atlas Concorde Rus",
             "collection_ref": "YO-C", "purchase": 999}]})
        self.pending = await self.store.get_pending(42)

    async def asyncTearDown(self):
        self._dir.cleanup()
        ph._files.pop(42, None)

    async def _press(self, action):
        chat = FakeMessage()
        chat.edit_reply_markup = lambda reply_markup=None: _noop()
        callback = type("C", (), {
            "data": f"price:{action}:{self.pending.proposal_id}",
            "from_user": type("U", (), {"id": 42})(), "message": chat,
            "answer": lambda *a, **k: _noop()})()
        orc = type("O", (), {"handle_turn": lambda *a, **k: _turn()})()
        await ph.handle_price_decision(callback, self.onec, self.store, None,
                                       is_admin=True, orchestrator=orc)
        return chat

    async def test_skip_moves_on_without_a_task(self):
        chat = await self._press("skip")
        self.assertIn("Пропущено", chat.text)
        self.assertEqual(await self.store.list_deferred(42), [])
        self.assertEqual([t["name"] for t in (await self.store.get_run(42))["remaining"]],
                         ["Azteca"])

    async def test_defer_records_the_task_and_saves_the_file(self):
        chat = await self._press("defer")
        self.assertIn("Отложено", chat.text)
        [row] = await self.store.list_deferred(42)
        self.assertEqual(row["tm_name"], "Atlas Concorde Rus")
        self.assertEqual(row["price_date"], "2026-08-26")
        self.assertTrue(Path(row["file_path"]).exists())

    async def test_defer_tm_from_tm_mode(self):
        """В режиме марок откладывается марка целиком — коллекция в задаче не указана."""
        chat = await self._press("defer_tm")
        self.assertIn("целиком", chat.text)
        [row] = await self.store.list_deferred(42)
        self.assertEqual(row["collection_ref"], "")

    async def test_defer_tm_from_collection_mode_closes_the_whole_tm(self):
        """Отложили марку, стоя на коллекции: остальные её коллекции не спрашиваем."""
        await self.store.start_stage(42, "T1", "Atlas Concorde Rus", [
            {"ref": "YO-C", "name": "Allure"}, {"ref": "YO-D", "name": "Drift"}])
        chat = await self._press("defer_tm")
        self.assertIn("целиком", chat.text)
        run = await self.store.get_run(42)
        self.assertIsNone(run["stage"])                       # очередь коллекций свёрнута
        self.assertEqual([t["name"] for t in run["remaining"]], ["Azteca"])
        [row] = await self.store.list_deferred(42)
        self.assertEqual(row["collection_ref"], "")

    async def test_defer_collection_keeps_the_tm_in_queue(self):
        await self.store.start_stage(42, "T1", "Atlas Concorde Rus", [
            {"ref": "YO-C", "name": "Allure"}, {"ref": "YO-D", "name": "Drift"}])
        await self._press("defer")
        run = await self.store.get_run(42)
        self.assertEqual([c["name"] for c in run["stage"]["remaining"]], ["Drift"])
        [row] = await self.store.list_deferred(42)
        self.assertEqual(row["collection_ref"], "YO-C")

    async def test_cancel_keeps_the_step(self):
        chat = await self._press("cancel")
        self.assertIn("Отменено", chat.text)
        self.assertEqual([t["name"] for t in (await self.store.get_run(42))["remaining"]],
                         ["Atlas Concorde Rus", "Azteca"])


class KeyboardTest(unittest.TestCase):
    """В режиме коллекций «Отложить» двусмысленна — поэтому кнопок две."""

    @staticmethod
    def _texts(markup):
        return [b.text for row in markup.inline_keyboard for b in row]

    @staticmethod
    def _actions(markup):
        return [b.callback_data.split(":")[1]
                for row in markup.inline_keyboard for b in row]

    def test_tm_mode_has_one_defer(self):
        markup = ph._keyboard(7, in_stage=False)
        self.assertEqual(self._actions(markup),
                         ["apply", "skip", "defer_tm", "cancel"])
        self.assertIn("🕐 Отложить марку целиком", self._texts(markup))

    def test_collection_mode_has_both(self):
        markup = ph._keyboard(7, in_stage=True)
        self.assertEqual(self._actions(markup),
                         ["apply", "skip", "defer", "defer_tm", "cancel"])
        self.assertIn("🕐 Отложить коллекцию", self._texts(markup))
        self.assertIn("🕐 Отложить марку", self._texts(markup))

    def test_cancel_explains_itself(self):
        """Иначе «Отмена» и «Пропустить» неотличимы на вид."""
        for stage in (False, True):
            self.assertIn("✖️ Отмена (остаться на текущей задаче)",
                          self._texts(ph._keyboard(7, in_stage=stage)))


async def _noop():
    return None


async def _turn():
    return "продолжаю", []


if __name__ == "__main__":
    unittest.main()
