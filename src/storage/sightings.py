"""Журнал встреч артикулов в прайсах поставщиков (§6.1 модели).

**Зачем.** «Позиции нет в этом прайсе» и «позиция снята с производства» — разные вещи, а
код до сих пор считал их одним: коллекция, которую просто перестал возить один поставщик,
выглядела снятой и уезжала в задачу на перенос. Журнал отвечает на единственный вопрос,
которого не хватало: **видели ли мы этот артикул в прайсе КОГО-ТО ЕЩЁ.**

**Токенов это не стоит нисколько.** Артикулы приносит сам разбор прайса — модель и так
перечисляет их в `compare_with_1c` по каждой коллекции, а сопоставление идёт по
нормализованному артикулу, то есть кодом. Ни одного лишнего вызова, ни одного лишнего
слова в истории.

**«Есть у другого» значит «есть в его ПОСЛЕДНЕМ прайсе».** Поэтому запись ведётся не
накоплением, а заменой: новый прайс той же пары (поставщик, сигнатура) стирает прежние
строки. Так поставщик, выкинувший коллекцию из свежего файла, перестаёт её «подтверждать»
сам собой — без сроков давности и догадок, сколько месяцев считать свежестью.

Сигнатура в ключе не для красоты: поставщик шлёт прайс ЧАСТЯМИ по типам товара («Ламинат»
отдельно, «Плитка» отдельно), и замена по одному лишь поставщику стёрла бы соседний
раздел, которого в этом файле и не могло быть.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_sighting (
    article_key   TEXT NOT NULL,          -- norm_article: «LE-263» и «le 263» — одно
    supplier_id   INTEGER NOT NULL,
    signature     TEXT NOT NULL DEFAULT '',
    supplier_name TEXT NOT NULL DEFAULT '',  -- денормализовано: строку читают для показа
    collection    TEXT NOT NULL DEFAULT '',  -- как коллекция названа В ПРАЙСЕ
    price_date    TEXT,                   -- дата прайса, где видели
    seen_at       TEXT NOT NULL,
    PRIMARY KEY (article_key, supplier_id, signature)
);
CREATE INDEX IF NOT EXISTS ix_sighting_article ON price_sighting (article_key);
"""

# Поставщик, который год не присылал прайсов, больше ничего не подтверждает: его строки
# перестают мешать снятию. Год — как у заявок на эксклюзив (`exclusive.CLAIM_TTL_DAYS`):
# прайсы приходят примерно ежемесячно, и живой поставщик подтверждает себя многократно.
SIGHTING_TTL_DAYS = 365


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Sighting:
    """Где ещё видели артикул."""
    supplier_id: int
    supplier: str
    collection: str
    price_date: str | None

    def label(self) -> str:
        where = self.supplier or f"поставщик №{self.supplier_id}"
        out = f"есть у «{where}»"
        if self.collection:
            out += f" в коллекции «{self.collection}»"
        if self.price_date:
            out += f" (прайс от {self.price_date})"
        return out


class SightingStore:
    def __init__(self, db_path) -> None:
        self._db_path = str(db_path)

    async def init(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def remember(self, supplier_id: int, signature: str, items: dict,
                       supplier: str = "", price_date: str | None = None) -> int:
        """Запомнить артикулы одного прайса. `items`: нормализованный артикул → коллекция.

        Замена, а не добавление: см. шапку модуля — строка обязана означать «есть в
        последнем прайсе», иначе выбывшая позиция подтверждалась бы вечно.
        """
        if not supplier_id:
            return 0
        rows = [(key, supplier_id, signature or "", supplier or "", name or "",
                 price_date, _now())
                for key, name in (items or {}).items() if key]
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "DELETE FROM price_sighting WHERE supplier_id = ? AND signature = ?",
                (supplier_id, signature or ""))
            if rows:
                await db.executemany(
                    "INSERT OR REPLACE INTO price_sighting (article_key, supplier_id, "
                    "signature, supplier_name, collection, price_date, seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            await db.commit()
        return len(rows)

    async def elsewhere(self, exclude_supplier_id: int = 0,
                        today: datetime | None = None) -> dict[str, Sighting]:
        """Всё, что видели у ОСТАЛЬНЫХ поставщиков: артикул → встреча.

        Отдаётся целиком, одним запросом и в память. Таблица маленькая (строка на пару
        артикул-поставщик), а спрашивать её по одной позиции пришлось бы из синхронного
        кода инструментов — и каждый раз заново.
        """
        edge = (today or datetime.now(timezone.utc)) - timedelta(days=SIGHTING_TTL_DAYS)
        out: dict[str, Sighting] = {}
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT article_key, supplier_id, supplier_name, collection, price_date, "
                "seen_at FROM price_sighting WHERE supplier_id <> ? ORDER BY seen_at",
                (exclude_supplier_id or 0,))
            for key, sid, name, collection, price_date, seen_at in await cur.fetchall():
                if (seen_at or "") < edge.isoformat():
                    continue
                out[key] = Sighting(supplier_id=sid, supplier=name or "",
                                    collection=collection or "", price_date=price_date)
        return out

    async def forget_supplier(self, supplier_id: int) -> int:
        """Убрать поставщика из журнала — например, когда его сливают с другим."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("DELETE FROM price_sighting WHERE supplier_id = ?",
                                   (supplier_id,))
            await db.commit()
            return cur.rowcount
