"""Персистентность модели работы с прайсами (§10 specs/agent-workflow-model.md).

Состояние пишется **при каждом изменении**: остановка VPS или сбой визуала не должны терять
данные и рассинхронизировать модель с тем, что видит админ.

**Таблицы нормализованные, а не сериализованный граф.** Под команду «выполни задачу N» нужен
адресуемый идентификатор, а не переписывание блоба целиком. Исключение одно — список имён
внутри `Ref`: это набор ЗНАЧЕНИЙ одного предмета, а не сущности, у него нет своей жизни и
адресовать его незачем. Он лежит колонкой JSON, и отдельная таблица на два-три слова только
усложнила бы чтение.

**Запись и пересчёт ссылок разделены.** `relink` (`src/model/price_list.py`) считает ссылки
над списком в памяти, `save_links` кладёт результат в базу одним заходом. Так правило
остаётся чистой функцией, которую видно целиком, и его можно проверить без базы.
"""
from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from src.model.enums import PriceStatus, TaskKind, TaskStatus, TaskSubject
from src.model.price import Price, SupplierPrice
from src.model.refs import Ref, TaskAddress, TradeMark
from src.model.task import PriceTask

_SCHEMA = """
CREATE TABLE IF NOT EXISTS price (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL,
    file_id     INTEGER NOT NULL,       -- запись в справочнике (supplier_price_file)
    file_path   TEXT NOT NULL,          -- без файла объект существовать не может (§3.1)
    filename    TEXT,
    signature   TEXT,                   -- вместе с supplier_id образует «тот же прайс»
    received_at TEXT,                   -- когда прайс пришёл агенту
    price_date  TEXT,                   -- дата ВНУТРИ файла, если её удалось прочитать
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    newer_id    INTEGER                 -- ссылка на САМЫЙ новый прайс группы, не на следующий
);
CREATE INDEX IF NOT EXISTS ix_price_group ON price (supplier_id, signature);
-- ТМ, найденные в прайсе. `in_1c = 0` — марки в 1С нет либо LLM не смогла сопоставить;
-- такая ТМ всё равно хранится: это факт о прайсе, который админу нужно видеть.
CREATE TABLE IF NOT EXISTS price_trade_mark (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    price_id INTEGER NOT NULL,
    name     TEXT NOT NULL,
    code     TEXT NOT NULL DEFAULT '',
    in_1c    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_tm_price ON price_trade_mark (price_id);
-- Задача по прайсу. Адрес разложен по колонкам, имена — JSON-массивом: их несколько
-- намеренно (§3.3), и совпадение ищется по ЛЮБОМУ непустому идентификатору.
CREATE TABLE IF NOT EXISTS price_task (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    price_id        INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    subject         TEXT NOT NULL,      -- коллекция | товар
    status          TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    result          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    done_at         TEXT,
    tm_code         TEXT NOT NULL DEFAULT '',
    tm_names        TEXT NOT NULL DEFAULT '[]',
    subject_code    TEXT NOT NULL DEFAULT '',
    subject_article TEXT NOT NULL DEFAULT '',
    subject_names   TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_task_price ON price_task (price_id);
"""


def _names(raw: str) -> tuple[str, ...]:
    try:
        return tuple(json.loads(raw or "[]"))
    except (ValueError, TypeError):
        return ()


def _dump(names) -> str:
    return json.dumps(list(names), ensure_ascii=False)


class ModelStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    # ----------------------------------------------------------------- чтение

    async def load_all(self) -> list[Price]:
        """Поднять весь список прайсов с задачами в память.

        Зовётся при старте: модель — синглтон и живёт в процессе, а база нужна, чтобы
        пережить его остановку.
        """
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, supplier_id, file_id, file_path, filename, signature, "
                "received_at, price_date, status, created_at, newer_id FROM price "
                "ORDER BY created_at, id")
            rows = await cur.fetchall()

            marks: dict[int, list[TradeMark]] = {}
            cur = await db.execute(
                "SELECT price_id, name, code, in_1c FROM price_trade_mark ORDER BY id")
            for price_id, name, code, in_1c in await cur.fetchall():
                marks.setdefault(price_id, []).append(
                    TradeMark(name=name, code=code, in_1c=bool(in_1c)))

            tasks: dict[int, list[PriceTask]] = {}
            cur = await db.execute(
                "SELECT id, price_id, kind, subject, status, description, result, "
                "created_at, done_at, tm_code, tm_names, subject_code, subject_article, "
                "subject_names FROM price_task ORDER BY created_at, id")
            for row in await cur.fetchall():
                tasks.setdefault(row[1], []).append(_task_from_row(row))

        out = []
        for row in rows:
            price = Price(
                id=row[0],
                supplier_price=SupplierPrice(
                    supplier_id=row[1], file_id=row[2], file_path=row[3],
                    filename=row[4] or "", signature=row[5] or "",
                    received_at=row[6], price_date=row[7],
                    trade_marks=marks.get(row[0], [])),
                status=PriceStatus(row[8]), created_at=row[9], newer_id=row[10],
                tasks=tasks.get(row[0], []))
            out.append(price)
        return out

    async def get_price(self, price_id: int) -> Price | None:
        return next((p for p in await self.load_all() if p.id == price_id), None)

    # ----------------------------------------------------------------- прайсы

    async def add_price(self, price: Price) -> Price:
        """Записать прайс целиком: сам, его ТМ и его задачи. Проставляет `id` на месте."""
        sp = price.supplier_price
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "INSERT INTO price (supplier_id, file_id, file_path, filename, signature, "
                "received_at, price_date, status, created_at, newer_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sp.supplier_id, sp.file_id, sp.file_path, sp.filename, sp.signature,
                 sp.received_at, sp.price_date, price.status.value, price.created_at,
                 price.newer_id))
            price.id = cur.lastrowid
            await self._write_marks(db, price)
            for task in price.tasks:
                await self._insert_task(db, price.id, task)
            await db.commit()
        return price

    async def set_price_status(self, price_id: int, status: PriceStatus) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("UPDATE price SET status = ? WHERE id = ?",
                                   (status.value, price_id))
            await db.commit()
            return cur.rowcount > 0

    async def save_links(self, prices: list[Price]) -> None:
        """Сохранить ссылки «на более свежий», посчитанные `relink` в памяти."""
        async with aiosqlite.connect(self._db_path) as db:
            for price in prices:
                if price.id is not None:
                    await db.execute("UPDATE price SET newer_id = ? WHERE id = ?",
                                     (price.newer_id, price.id))
            await db.commit()

    async def set_trade_marks(self, price: Price) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._write_marks(db, price)
            await db.commit()

    async def remove_price(self, price_id: int) -> str | None:
        """Уничтожить прайс вместе с задачами и ТМ. Возвращает путь к файлу.

        Ссылки других прайсов на уничтоженный **снимаются здесь же**, чтобы в базе не
        осталось указателя в пустоту. Правильные ссылки вызывающий пересчитает `relink` и
        положит `save_links` — здесь мы только не оставляем мусор.
        """
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT file_path FROM price WHERE id = ?", (price_id,))
            row = await cur.fetchone()
            if not row:
                return None
            await db.execute("DELETE FROM price_task WHERE price_id = ?", (price_id,))
            await db.execute("DELETE FROM price_trade_mark WHERE price_id = ?", (price_id,))
            await db.execute("UPDATE price SET newer_id = NULL WHERE newer_id = ?",
                             (price_id,))
            await db.execute("DELETE FROM price WHERE id = ?", (price_id,))
            await db.commit()
            return row[0]

    # ------------------------------------------------------------------ задачи

    async def add_task(self, price_id: int, task: PriceTask) -> PriceTask:
        async with aiosqlite.connect(self._db_path) as db:
            await self._insert_task(db, price_id, task)
            await db.commit()
        return task

    async def update_task(self, task: PriceTask) -> bool:
        """Записать текущее состояние задачи целиком.

        Одним запросом на все поля, а не по одному на каждое: меняются они пачками (статус
        вместе с результатом и датой, адрес вместе с описанием при дополнении), и дробить
        это на пять обновлений значило бы дать состоянию побыть противоречивым.
        """
        if task.id is None:
            raise ValueError("нельзя обновить задачу без id")
        addr = task.address
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE price_task SET kind = ?, subject = ?, status = ?, description = ?, "
                "result = ?, done_at = ?, tm_code = ?, tm_names = ?, subject_code = ?, "
                "subject_article = ?, subject_names = ? WHERE id = ?",
                (task.kind.value, task.subject.value, task.status.value, task.description,
                 task.result, task.done_at, addr.tm.code, _dump(addr.tm.names),
                 addr.subject.code, addr.subject.article, _dump(addr.subject.names),
                 task.id))
            await db.commit()
            return cur.rowcount > 0

    async def remove_task(self, task_id: int) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("DELETE FROM price_task WHERE id = ?", (task_id,))
            await db.commit()
            return cur.rowcount > 0

    async def replace_tasks(self, price_id: int, tasks: list[PriceTask]) -> None:
        """Пересборка (§6.3): старые задачи уничтожаются, новые пишутся с нуля.

        Ничего не переносится — ни статусы, ни правки описаний. Старые `id` не
        переиспользуются: команда «выполни задачу 7» после пересборки должна не найти
        задачу, а не выполнить другую, оказавшуюся под тем же номером.
        """
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM price_task WHERE price_id = ?", (price_id,))
            for task in tasks:
                task.id = None
                await self._insert_task(db, price_id, task)
            await db.commit()

    # ---------------------------------------------------------------- утилиты

    async def known_paths(self) -> set[str]:
        """Файлы, на которые ссылается модель, — для уборки сирот при старте."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT file_path FROM price")
            return {row[0] for row in await cur.fetchall() if row[0]}

    async def _write_marks(self, db, price: Price) -> None:
        await db.execute("DELETE FROM price_trade_mark WHERE price_id = ?", (price.id,))
        for mark in price.supplier_price.trade_marks:
            await db.execute(
                "INSERT INTO price_trade_mark (price_id, name, code, in_1c) "
                "VALUES (?, ?, ?, ?)",
                (price.id, mark.name, mark.code, 1 if mark.in_1c else 0))

    async def _insert_task(self, db, price_id: int, task: PriceTask) -> None:
        addr = task.address
        cur = await db.execute(
            "INSERT INTO price_task (price_id, kind, subject, status, description, result, "
            "created_at, done_at, tm_code, tm_names, subject_code, subject_article, "
            "subject_names) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (price_id, task.kind.value, task.subject.value, task.status.value,
             task.description, task.result, task.created_at, task.done_at,
             addr.tm.code, _dump(addr.tm.names), addr.subject.code,
             addr.subject.article, _dump(addr.subject.names)))
        task.id = cur.lastrowid


def _task_from_row(row) -> PriceTask:
    """Собрать задачу обратно из строки. Имена уже нормализованы при записи."""
    (task_id, _price_id, kind, subject, status, description, result,
     created_at, done_at, tm_code, tm_names, subj_code, subj_article, subj_names) = row

    address = TaskAddress(
        tm=Ref(code=tm_code, names=_names(tm_names)),
        subject=Ref(code=subj_code, article=subj_article, names=_names(subj_names)),
        subject_kind=TaskSubject(subject))

    task = PriceTask(kind=TaskKind(kind), address=address, description=description,
                     status=TaskStatus(status), result=result, id=task_id,
                     created_at=created_at, done_at=done_at)
    return task
