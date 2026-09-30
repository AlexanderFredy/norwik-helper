"""Журнал наблюдений за фото и еженедельное напоминание (решение админа 30.09.2026).

Снимок «48 без фото» не отличает «ничего не делали» от «двенадцать закрыли, двенадцать
новых завели». Прогресс — это история, и ведёт её журнал: строка на товар.
"""
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from src.bot import photo_daily
from src.storage.photo_watch import PhotoWatchStore
from src.website_tool import photo_report, photo_scan, photos


def seen(ref="T1", site_id="1001", tm="Classen", collection="Manor", name="Вернон",
         created="2026-08-01", state=photos.NONE):
    return {"ref": ref, "site_id": site_id, "tm": tm, "collection": collection,
            "name": name, "created": created, "state": state}


class StoreTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PhotoWatchStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_new_observation_starts_a_row(self):
        await self.store.observe([seen()], today="2026-09-30")
        rows = await self.store.waiting()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].ref, "T1")
        self.assertIsNone(rows[0].photo_at, "фото ещё нет")
        self.assertEqual(rows[0].first_seen, "2026-09-30")

    async def test_photo_from_the_very_first_look_has_no_closing_date(self):
        """Иначе первый же прогон объявит сегодняшним днём всё, что сделали до нас: в
        журнал разом попадает сотня позиций с давно стоящим фото, и отчёт отрапортует
        «за неделю добавлено 66» — число, выдуманное моментом запуска."""
        await self.store.observe([seen(state=photos.HAS)], today="2026-09-10")
        rows = await self.store._select()
        self.assertIsNone(rows[0].photo_at)
        self.assertEqual(rows[0].last_state, photos.HAS, "но и ждущим он не считается")
        self.assertEqual(await self.store.waiting(), [])

    async def test_closing_date_is_the_transition(self):
        await self.store.observe([seen(state=photos.NONE)], today="2026-09-10")
        await self.store.observe([seen(state=photos.HAS)], today="2026-09-20")
        await self.store.observe([seen(state=photos.HAS)], today="2026-09-30")
        rows = await self.store._select()
        self.assertEqual(rows[0].photo_at, "2026-09-20", "дата перехода и не сдвигается")

    async def test_photo_that_vanished_is_waiting_again(self):
        """Ждёт ли товар фото СЕЙЧАС — вопрос к последнему вердикту, а не к photo_at."""
        await self.store.observe([seen(state=photos.NONE)], today="2026-09-05")
        await self.store.observe([seen(state=photos.HAS)], today="2026-09-10")
        await self.store.observe([seen(state=photos.NONE)], today="2026-09-30")
        waiting = await self.store.waiting()
        self.assertEqual([w.ref for w in waiting], ["T1"])
        self.assertEqual(waiting[0].photo_at, "2026-09-10", "срок не потерян")

    async def test_first_seen_does_not_move(self):
        await self.store.observe([seen()], today="2026-09-10")
        await self.store.observe([seen()], today="2026-09-30")
        self.assertEqual((await self.store.waiting())[0].first_seen, "2026-09-10")

    async def test_empty_site_id_does_not_erase_a_known_one(self):
        """id сайта появляется позже карточки: пустой — это «пока не знаем», а не «нет»."""
        await self.store.observe([seen(site_id="1001")], today="2026-09-10")
        await self.store.observe([seen(site_id="")], today="2026-09-30")
        self.assertEqual((await self.store.waiting())[0].site_id, "1001")

    async def test_row_without_ref_is_skipped(self):
        """Без кода 1С строку не с чем связать — следующее наблюдение завело бы дубль."""
        await self.store.observe([seen(ref="")], today="2026-09-30")
        self.assertEqual(await self.store.waiting(), [])

    async def test_stale_counts_from_the_1c_date(self):
        """От даты заведения, а не от первого наблюдения: иначе позиции, просроченные к
        моменту запуска журнала, ждали бы первого напоминания ещё месяц."""
        await self.store.observe([seen(ref="A", created="2026-08-01"),
                                  seen(ref="B", created="2026-09-28")],
                                 today="2026-09-30")
        stale = await self.store.stale(days=30, today="2026-09-30")
        self.assertEqual([w.ref for w in stale], ["A"])
        self.assertEqual(stale[0].waiting_days("2026-09-30"), 60)

    async def test_open_ids_skips_those_with_photo_and_idless(self):
        """Признак — последний вердикт: у позиции, пришедшей с фото сразу, дата закрытия
        пуста навсегда, и по ней мы перепроверяли бы её вечно."""
        await self.store.observe([seen(ref="A", site_id="1"),
                                  seen(ref="B", site_id="2", state=photos.HAS),
                                  seen(ref="C", site_id="")], today="2026-09-30")
        self.assertEqual([w.ref for w in await self.store.open_ids()], ["A"])

    async def close(self, ref, created, waited_until, seen_at):
        """Позицию сперва увидели без фото, потом с фото — только так считается закрытие."""
        await self.store.observe([seen(ref=ref, created=created)], today=seen_at)
        await self.store.observe([seen(ref=ref, created=created, state=photos.HAS)],
                                 today=waited_until)

    async def test_progress_counts_what_moved(self):
        await self.store.observe([
            seen(ref="A", created="2026-08-01"),                       # ждёт, просрочен
            seen(ref="B", created="2026-09-28"),                       # ждёт, свежий
        ], today="2026-09-30")
        await self.close("C", "2026-09-01", "2026-09-26", "2026-09-20")
        await self.close("D", "2026-08-20", "2026-09-05", "2026-09-01")

        p = await self.store.progress(today="2026-09-30", days=30)
        self.assertEqual((p.waiting, p.stale), (2, 1))
        self.assertEqual(p.closed_week, 1, "C закрыт четыре дня назад")
        self.assertEqual(p.closed_month, 2, "C и D — оба внутри месяца")
        self.assertEqual(p.watched, 4)

    async def test_median_not_average(self):
        """Одна карточка, забытая на полгода, сдвинула бы среднее так, что оно перестало
        бы описывать обычный срок."""
        await self.close("A", "2026-09-01", "2026-09-03", "2026-09-02")
        await self.close("B", "2026-09-01", "2026-09-05", "2026-09-02")
        await self.close("C", "2026-01-01", "2026-09-30", "2026-09-02")
        self.assertEqual((await self.store.progress(today="2026-09-30")).median_days, 4)

    async def test_empty_journal_says_nothing_happened(self):
        p = await self.store.progress(today="2026-09-30")
        self.assertTrue(p.quiet)

    async def test_last_run(self):
        self.assertEqual(await self.store.last_run(), "")
        await self.store.observe([seen()], today="2026-09-30")
        self.assertEqual(await self.store.last_run(), "2026-09-30")

    async def test_forget(self):
        await self.store.observe([seen(ref="A"), seen(ref="B")], today="2026-09-30")
        await self.store.forget(["A"])
        self.assertEqual([w.ref for w in await self.store.waiting()], ["B"])


class DigestTest(unittest.IsolatedAsyncioTestCase):
    """Дайджест — строка на МАРКУ, без поимённого списка (решение админа 30.09.2026)."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PhotoWatchStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def text(self, rows, today="2026-09-30"):
        await self.store.observe(rows, today=today)
        return photo_report.digest(await self.store.progress(today=today),
                                   await self.store.waiting(), today=today)

    async def test_nothing_waiting_means_no_message(self):
        """Еженедельное «всё в порядке» через месяц перестают читать вместе с теми
        письмами, в которых что-то есть."""
        self.assertIsNone(await self.text([seen(state=photos.HAS)]))

    async def test_line_per_mark_with_totals(self):
        rows = [seen(ref="A", tm="Classen", created="2026-08-01"),
                seen(ref="B", tm="Classen", created="2026-09-28"),
                seen(ref="C", tm="Peli", created="2026-09-28")]
        text = await self.text(rows)
        self.assertIn("Всего ждут фото: 3 поз.", text)
        self.assertIn("- Classen — 2, из них больше месяца — 1", text)
        self.assertIn("- Peli — 1", text)

    async def test_zero_overdue_is_not_written_out(self):
        """Приписка «— 0» ничего не сообщает, а глаз за неё цепляется наравне с числами."""
        text = await self.text([seen(tm="Peli", created="2026-09-28")])
        self.assertIn("- Peli — 1", text)
        self.assertNotIn("больше месяца", text)

    async def test_no_names_and_no_links(self):
        """Поимённый список в еженедельном письме не нужен — для него есть вопрос агенту."""
        text = await self.text([seen(name="Вернон", created="2026-08-01")])
        self.assertNotIn("Вернон", text)
        self.assertNotIn("https://", text)

    async def test_marks_with_overdue_come_first(self):
        rows = [seen(ref=f"S{i}", tm="Без просрочки", created="2026-09-28")
                for i in range(5)]
        rows.append(seen(ref="L", tm="С просрочкой", created="2026-01-01"))
        text = await self.text(rows)
        lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
        self.assertTrue(lines[0].startswith("- С просрочкой"))

    async def test_progress_numbers_are_there(self):
        await self.store.observe([seen(ref="B", created="2026-09-01")],
                                 today="2026-09-20")
        await self.store.observe([seen(ref="B", created="2026-09-01",
                                       state=photos.HAS)], today="2026-09-25")
        text = await self.text([seen(ref="A", created="2026-08-01")])
        self.assertIn("Фото добавлено: за неделю 1", text)

    async def test_no_movement_no_line(self):
        """Журнал только завели — рассказывать о динамике нечего, и выдумывать её нельзя."""
        text = await self.text([seen(ref="A", created="2026-08-01"),
                                seen(ref="B", created="2026-08-01", state=photos.HAS)])
        self.assertNotIn("Фото добавлено", text)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class FakeSubs:
    """Список подписчиков. Пустой значит «никому», а не «всем»."""

    def __init__(self, ids=()):
        self._ids = list(ids)

    async def list_all(self):
        from src.storage.photo_subscribers import Subscriber
        return [Subscriber(telegram_id=i, name=f"user{i}", added_by=1, added_at="")
                for i in self._ids]


class DailyTest(unittest.IsolatedAsyncioTestCase):
    """Проверка ежедневная, дайджест еженедельный — это разные вещи."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PhotoWatchStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    def scan_result(self, observations=None, problem=""):
        out = photo_scan.Scan(problem=problem)
        out.observations = observations if observations is not None else [seen()]
        out.checked = len(out.observations)
        return out

    async def run_day(self, day, result=None, bot=None, subs=None):
        async def fake_scan(_onec, **_kw):
            return result or self.scan_result()

        with patch.object(photo_daily.photo_scan, "scan", fake_scan):
            await photo_daily.run_once(object(), self.store, bot,
                                       subs if subs is not None else FakeSubs([100]),
                                       today=day)

    async def test_workday_writes_the_journal_and_stays_silent(self):
        bot = FakeBot()
        await self.run_day(date(2026, 9, 30), bot=bot)      # среда
        self.assertEqual(len(await self.store.waiting()), 1)
        self.assertEqual(bot.sent, [], "по будням не рассылаем")

    async def test_monday_sends_the_digest_to_every_subscriber(self):
        bot = FakeBot()
        result = self.scan_result([seen(ref="A", created="2026-08-01")])
        await self.run_day(date(2026, 9, 28), result, bot,   # понедельник
                           subs=FakeSubs([100, 200]))
        self.assertEqual([chat for chat, _ in bot.sent], [100, 200])
        self.assertIn("Всего ждут фото: 1 поз.", bot.sent[0][1])

    async def test_empty_subscriber_list_means_nobody(self):
        """Пустой список — «не слать никому», в том числе админу: обратное умолчание
        однажды разошлёт каталог всем подряд."""
        bot = FakeBot()
        result = self.scan_result([seen(ref="A", created="2026-08-01")])
        await self.run_day(date(2026, 9, 28), result, bot, subs=FakeSubs([]))
        self.assertEqual(bot.sent, [])

    async def test_monday_without_anyone_waiting_sends_nothing(self):
        bot = FakeBot()
        result = self.scan_result([seen(state=photos.HAS)])
        await self.run_day(date(2026, 9, 28), result, bot)
        self.assertEqual(bot.sent, [])

    async def test_broken_scan_does_not_touch_the_journal(self):
        """Сломанный обход не имеет права стереть историю — и подписчики должны узнать
        причину, иначе прогресс замрёт, а понять почему будет негде."""
        bot = FakeBot()
        await self.run_day(date(2026, 9, 30),
                           self.scan_result(problem="1С не отдаёт дату создания"), bot)
        self.assertEqual(await self.store.waiting(), [])
        self.assertIn("не отдаёт дату создания", bot.sent[0][1])

    async def test_open_items_are_rechecked_even_out_of_the_window(self):
        """Товар, заведённый давно и до сих пор без фото, пропал бы из виду ровно тогда,
        когда стал самым просроченным."""
        await self.store.observe([seen(ref="OLD", site_id="777",
                                       created="2026-01-01")], today="2026-09-29")
        got = {}

        async def fake_scan(_onec, **kw):
            got.update(kw)
            return self.scan_result([])

        with patch.object(photo_daily.photo_scan, "scan", fake_scan):
            await photo_daily.run_once(object(), self.store, None, None,
                                       today=date(2026, 9, 30))
        self.assertEqual([e["ref"] for e in got["extra"]], ["OLD"])


class ScheduleTest(unittest.TestCase):

    def test_next_run_is_today_when_the_hour_has_not_come(self):
        run = photo_daily.next_run(datetime(2026, 9, 30, 3, 0), hour=7)
        self.assertEqual(run, datetime(2026, 9, 30, 7, 0))

    def test_next_run_moves_to_tomorrow(self):
        run = photo_daily.next_run(datetime(2026, 9, 30, 9, 0), hour=7)
        self.assertEqual(run, datetime(2026, 10, 1, 7, 0))

    def test_exactly_at_the_hour_means_tomorrow(self):
        """Строго ПОСЛЕ: иначе проверка, начавшаяся в 7:00, тут же запустилась бы снова."""
        run = photo_daily.next_run(datetime(2026, 9, 30, 7, 0), hour=7)
        self.assertEqual(run, datetime(2026, 10, 1, 7, 0))


if __name__ == "__main__":
    unittest.main()
