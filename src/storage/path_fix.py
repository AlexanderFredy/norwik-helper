"""Пути к прайсам в базе — к прямым слэшам (переезд на VPS, 08.10.2026).

База жила на Windows и хранит «data\\prices\\x.xlsx». На Linux обратный слэш — обычный символ
имени, и такой путь не указывает ни на что: ни прочитать прайс, ни узнать, что файл занят,
а уборка сирот стёрла бы весь каталог. Поэтому при каждом старте, ДО уборки, пути во всех
хранилищах приводятся к прямым слэшам. Идемпотентно; Windows прямые слэши понимает.

Таблицы перечислены явно, а не найдены по имени колонки: правка путей в чужой таблице —
не та операция, которую стоит делать по догадке.
"""
from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger(__name__)

#: (таблица, колонка) — всё, где лежит путь к файлу прайса.
COLUMNS = (
    ("price", "file_path"),
    ("supplier_price_file", "path"),
    ("deferred_tasks", "file_path"),
    ("price_queue", "path"),
    ("active_price", "path"),
)


async def normalize(db_path) -> int:
    """Заменить обратные слэши прямыми. Возвращает число исправленных строк."""
    fixed = 0
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        tables = {row[0] for row in await cur.fetchall()}
        for table, column in COLUMNS:
            if table not in tables:
                continue
            # OR IGNORE — ради UNIQUE у `supplier_price_file.path`: если тот же файл уже
            # записан и прямыми слэшами, старая строка остаётся как есть, а читается она
            # всё равно верно (`price_files.to_path`).
            cur = await db.execute(
                f"UPDATE OR IGNORE {table} SET {column} = REPLACE({column}, '\\', '/') "
                f"WHERE {column} LIKE '%\\%'")
            fixed += cur.rowcount or 0
        await db.commit()
    if fixed:
        logger.info("Пути к прайсам приведены к прямым слэшам: %d строк", fixed)
    return fixed
