"""Журнал наблюдений за фото новых товаров (решение админа 30.09.2026).

**ЗАЧЕМ ЖУРНАЛ, ЕСЛИ ЕСТЬ ПРОВЕРКА.** `find_items_without_photo` отвечает СНИМКОМ: «48 без
фото». По снимку нельзя сказать, стало лучше или хуже, — 48 сегодня и 48 неделю назад
одинаково означают и «ничего не делали», и «двенадцать закрыли, двенадцать новых завели».
Прогресс это история, и вести её больше негде.

**СТРОКА НА ТОВАР, А НЕ ЧИСЛО НА ДЕНЬ.** Дневной итог («ждут 48») занимает меньше места, но
не отвечает ни на один вопрос, который задают на самом деле: сколько ждут дольше месяца,
сколько закрыли за неделю, сколько дней проходит от заведения до фото, кто ждёт дольше
всех. Всё это выводится из строки на товар и ниоткуда больше.

**ЖУРНАЛ ПОМНИТ ПОЗИЦИЮ, КОГДА ОНА ВЫПАЛА ИЗ ОКНА.** Проверка смотрит товары за последние
три месяца. Товар, заведённый четыре месяца назад и до сих пор без фото, перестал бы
попадать в выборку ровно тогда, когда стал самым просроченным, — и исчез бы молча. Поэтому
однажды взятая на заметку позиция остаётся здесь, пока фото не появится, а перепроверять её
можно по id сайта, не спрашивая 1С вовсе.

**`photo_at` — ЭТО ПЕРЕХОД «НЕ БЫЛО → ПОЯВИЛОСЬ», А НЕ «ФОТО ЕСТЬ».** Разница решающая, и
она видна с первого же прогона: в журнал разом попадают сто позиций, у которых фото стояло
давно, и если считать их закрытыми сегодня, отчёт отрапортует «за неделю добавлено 66» —
число, которое целиком выдумано моментом запуска журнала. Поэтому дата ставится ТОЛЬКО
когда мы своими глазами видели позицию без фото, а потом с фото. Фото, которое было с
первого наблюдения, оставляет `photo_at` пустым: когда его сделали, не знает никто, и
честный пробел здесь лучше правдоподобной даты.

Ждёт ли товар фото СЕЙЧАС — отдельный вопрос, и на него отвечает `last_state`. Смешать эти
два — значит потерять срок при первой же переделке карточки.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import aiosqlite

from src.website_tool import photos

_SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_watch (
    ref          TEXT PRIMARY KEY,        -- код 1С: он есть всегда и не меняется
    site_id      TEXT NOT NULL DEFAULT '',-- id сайта; у невыгруженного пуст, появится позже
    tm           TEXT NOT NULL DEFAULT '',
    collection   TEXT NOT NULL DEFAULT '',
    name         TEXT NOT NULL DEFAULT '',
    created      TEXT NOT NULL DEFAULT '',-- ДатаСоздания в 1С, ГГГГ-ММ-ДД
    first_seen   TEXT NOT NULL,           -- когда позицию впервые взяли на заметку
    photo_at     TEXT,                    -- когда ВПЕРВЫЕ увидели фото; NULL — ещё нет
    last_state   TEXT NOT NULL DEFAULT '',-- последний вердикт: есть | нет | нет карточки | …
    last_checked TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_photo_watch_open ON photo_watch (last_state, created);
"""

#: Сколько дней без фото считаем просрочкой. Месяц — срок админа (30.09.2026): за месяц
#: фото успевают сделать и загрузить даже при очереди.
STALE_DAYS = 30


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _days_between(early: str, late: str) -> int | None:
    """Дней между двумя ГГГГ-ММ-ДД. None — какой-то из дат нет."""
    try:
        return (date.fromisoformat(late) - date.fromisoformat(early)).days
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class Watched:
    """Строка журнала в том виде, в каком её читает отчёт."""
    ref: str
    site_id: str
    tm: str
    collection: str
    name: str
    created: str
    first_seen: str
    photo_at: str | None
    last_state: str
    last_checked: str

    def waiting_days(self, today: str | None = None) -> int | None:
        """Сколько дней товар ждёт фото. Считаем ОТ ДАТЫ ЗАВЕДЕНИЯ В 1С, а не от первого
        наблюдения: иначе позиции, просроченные к моменту запуска журнала, получили бы
        первое напоминание только через месяц."""
        return _days_between(self.created, today or _today())


@dataclass(frozen=True)
class Progress:
    """Числа, ради которых журнал и заведён."""
    waiting: int = 0           # ждут фото сейчас
    stale: int = 0             # из них дольше STALE_DAYS
    closed_week: int = 0       # фото появилось за последние 7 дней
    closed_month: int = 0      # за последние 30
    median_days: int | None = None   # от заведения до фото, по закрытым
    watched: int = 0           # всего под наблюдением

    @property
    def quiet(self) -> bool:
        """Рассказывать не о чем: ни ожидающих, ни закрытых."""
        return not (self.waiting or self.closed_month)


class PhotoWatchStore:
    def __init__(self, db_path) -> None:
        self._db_path = str(db_path)

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def observe(self, rows: list[dict], today: str | None = None) -> int:
        """Записать наблюдения пачкой. `rows`: ref, site_id, tm, collection, name,
        created, state.

        Пишем ОДНИМ ЗАПРОСОМ на позицию и через UPSERT: наблюдений сотни в день, и цикл
        «прочитать-решить-записать» стоил бы трёх обращений к базе на каждое.

        `first_seen` ставится только при первом появлении (`COALESCE` не годится — строки
        ещё нет, поэтому это делает сам INSERT).

        `photo_at` ставится ТОЛЬКО на переходе «ждал → появилось» и больше не меняется. У
        новой строки он пуст ВСЕГДА, даже когда фото уже есть: иначе первый же прогон
        объявил бы сегодняшним днём всё, что сделали до нас.
        """
        if not rows:
            return 0
        stamp = today or _today()
        payload = []
        for row in rows:
            ref = str(row.get("ref") or "").strip()
            if not ref:
                # Без кода 1С строку не с чем связать: следующее наблюдение завело бы
                # дубль, и счётчики поехали бы.
                continue
            state = str(row.get("state") or "")
            payload.append((
                ref, str(row.get("site_id") or ""), str(row.get("tm") or ""),
                str(row.get("collection") or ""), str(row.get("name") or ""),
                str(row.get("created") or ""), stamp,
                None,                      # photo_at у новой строки пуст всегда
                state, stamp,
                # Два последних — для CASE в UPDATE: переход считается только из «нет».
                photos.HAS, photos.NONE))

        async with aiosqlite.connect(self._db_path) as db:
            await db.executemany(
                "INSERT INTO photo_watch (ref, site_id, tm, collection, name, created,"
                " first_seen, photo_at, last_state, last_checked)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(ref) DO UPDATE SET"
                "   site_id = CASE WHEN excluded.site_id <> '' THEN excluded.site_id"
                "                  ELSE photo_watch.site_id END,"
                "   tm = excluded.tm, collection = excluded.collection,"
                "   name = excluded.name, created = excluded.created,"
                # Дата закрытия — только на переходе «ждал → появилось». Уже стоящую не
                # трогаем: она мера срока и обязана пережить любые переделки карточки.
                "   photo_at = COALESCE(photo_watch.photo_at,"
                "     CASE WHEN excluded.last_state = ? AND photo_watch.last_state = ?"
                "          THEN excluded.last_checked END),"
                "   last_state = excluded.last_state,"
                "   last_checked = excluded.last_checked",
                payload)
            await db.commit()
        return len(payload)

    async def _select(self, where: str = "", params: tuple = ()) -> list[Watched]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT ref, site_id, tm, collection, name, created, first_seen,"
                " photo_at, last_state, last_checked FROM photo_watch "
                + where, params)
            return [Watched(**dict(r)) for r in await cur.fetchall()]

    async def waiting(self) -> list[Watched]:
        """Кто сейчас без фото. Именно `last_state`, а не «photo_at пуст»: товар, у
        которого фото было и пропало, тоже ждёт."""
        return await self._select("WHERE last_state = ? ORDER BY created", (photos.NONE,))

    async def open_ids(self) -> list[Watched]:
        """Позиции, которые надо перепроверять на сайте, даже если 1С их уже не отдаёт как
        новые: всё, где фото пока нет.

        Признак — `last_state`, а не пустой `photo_at`: у позиции, пришедшей с фото сразу,
        дата закрытия пуста навсегда, и по ней мы перепроверяли бы её до скончания века.
        """
        return await self._select(
            "WHERE last_state <> ? AND site_id <> '' ORDER BY created", (photos.HAS,))

    async def stale(self, days: int = STALE_DAYS,
                    today: str | None = None) -> list[Watched]:
        """Просроченные — дольше `days` без фото, от старых к новым."""
        stamp = today or _today()
        return [w for w in await self.waiting()
                if (w.waiting_days(stamp) or 0) >= days]

    async def progress(self, today: str | None = None,
                       days: int = STALE_DAYS) -> Progress:
        stamp = today or _today()
        rows = await self._select()
        if not rows:
            return Progress()

        waiting = [w for w in rows if w.last_state == photos.NONE]
        closed = [w for w in rows if w.photo_at]

        def closed_since(back: int) -> int:
            edge = (date.fromisoformat(stamp) - timedelta(days=back)).isoformat()
            return sum(1 for w in closed if (w.photo_at or "") >= edge)

        # МЕДИАНА, А НЕ СРЕДНЕЕ. Одна карточка, забытая на полгода, сдвигает среднее так,
        # что оно перестаёт описывать обычный срок, — а рассказать надо именно про обычный.
        spans = sorted(d for d in (_days_between(w.created, w.photo_at or "")
                                   for w in closed) if d is not None and d >= 0)
        median = spans[len(spans) // 2] if spans else None

        return Progress(
            waiting=len(waiting),
            stale=sum(1 for w in waiting if (w.waiting_days(stamp) or 0) >= days),
            closed_week=closed_since(7), closed_month=closed_since(30),
            median_days=median, watched=len(rows))

    async def last_run(self) -> str:
        """День последнего наблюдения, ГГГГ-ММ-ДД. Пусто — журнал ещё не наполняли.

        По нему при старте решается, догонять ли сегодняшнюю проверку: бота перезапускают
        среди дня, и привязка только к часу означала бы пропущенный день.
        """
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT MAX(last_checked) FROM photo_watch")
            row = await cur.fetchone()
            return (row[0] or "") if row else ""

    async def forget(self, refs: list[str]) -> int:
        """Убрать позиции из журнала — их не стало в 1С (удалили, пометили на удаление).
        Держать их дальше значит напоминать о работе, которой нет."""
        if not refs:
            return 0
        async with aiosqlite.connect(self._db_path) as db:
            await db.executemany("DELETE FROM photo_watch WHERE ref = ?",
                                 [(r,) for r in refs])
            await db.commit()
        return len(refs)
