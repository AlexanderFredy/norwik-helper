"""Справочники поставщиков, сигнатур и файлов прайсов (§2 spec/agent-workflow-model.md).

Три уровня, иерархия строгая:

    supplier              поставщик
     └── signature        сигнатура формата — у одного поставщика их несколько
          └── price_file  файл прайса на сервере

**Зачем справочник вообще.** До него поставщик был свободной строкой, которую называла LLM, и
она ложилась в поля `supplier` семи таблиц. Вывод нигде не закреплялся: на следующем прайсе
опознание начиналось заново, а поправить его админ мог только фразой в диалоге.

**Почему у поставщика несколько сигнатур.** Он дробит прайс по типам товаров («ламинат»
отдельно, «плитка» отдельно) и меняет формат. Каждая сигнатура — отдельный скелет файла.

**Почему сигнатура НЕ уникальна сама по себе.** Скелеты разных поставщиков могут случайно
совпасть, поэтому уникальна пара (поставщик, сигнатура), а не хеш. Любой поиск по сигнатуре
может вернуть несколько кандидатов из разных поставщиков — выбирает LLM по остальным
признакам, а ошибку админ правит командами справочника.

Историю строковых `supplier` из старых таблиц сюда НЕ переносим — справочники заводятся с
нуля (§2.4 спеки).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from src.price_tool.scope import normalize

_SCHEMA = """
CREATE TABLE IF NOT EXISTS supplier (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,           -- как показываем админу
    name_norm  TEXT NOT NULL UNIQUE,    -- по нему ищем: регистр и пунктуация не должны плодить дубли
    created_at TEXT NOT NULL
);
-- Сигнатура формата прайса. UNIQUE по ПАРЕ, а не по хешу: скелеты разных поставщиков могут
-- совпасть случайно, и тогда это две разные записи у двух разных владельцев.
CREATE TABLE IF NOT EXISTS supplier_signature (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL,
    signature   TEXT NOT NULL,          -- хеш скелета из price_signature()
    sample_name TEXT,                   -- имя файла, на котором сигнатуру впервые увидели
    purpose     TEXT,                   -- «ламинат», «плитка» — чем этот прайс отличается
    -- КАКИЕ ЛИСТЫ РАЗБИРАТЬ. Пусто = все. Это УКАЗАНИЕ АДМИНА, а не наблюдение: у FLOOR
    -- SERVICE четырнадцать листов, из них по делу два-три, а разбор каждого лишнего стоит
    -- и токенов, и кругов цикла. Живёт у СИГНАТУРЫ, потому что это свойство формата:
    -- следующий файл того же поставщика придёт с теми же листами.
    sheets      TEXT,
    -- ВСЕ листы, какие были в последнем файле этого формата. Нужны, чтобы было ИЗ ЧЕГО
    -- выбирать: сами имена знает только файл, и без этой памяти ни команда, ни форма не
    -- могут показать список — админу пришлось бы набирать имена по памяти.
    sheet_list  TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    UNIQUE (supplier_id, signature)
);
CREATE INDEX IF NOT EXISTS ix_signature_hash ON supplier_signature (signature);
CREATE TABLE IF NOT EXISTS supplier_price_file (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signature_id INTEGER NOT NULL,
    filename     TEXT NOT NULL,
    path         TEXT NOT NULL UNIQUE,  -- путь на диске: дважды один файл не регистрируем
    received_at  TEXT,                  -- когда прайс пришёл агенту
    added_at     TEXT NOT NULL,         -- когда завели запись
    UNIQUE (signature_id, path)
);
CREATE INDEX IF NOT EXISTS ix_price_file_signature ON supplier_price_file (signature_id);
-- БРЕНДЫ ВНУТРИ ЛИСТА (решение админа 03.10.2026). Второй список у формата, рядом с
-- `sheets`: у «Остатков» один лист на 1286 строк и 23 бренда, выбор листов там бесполезен,
-- а 13 брендов из 23 в справочнике 1С отсутствуют вовсе — это не экономия, это отделение
-- работы от мусора. К разбору уходит ПЕРЕСЕЧЕНИЕ отмеченных листов и отмеченных брендов.
--
-- СТРОКА НА БРЕНД, а не строка через запятую, как `sheets`: у бренда помимо имени ТРИ
-- значения — флажок, марка 1С и скидка от розницы, — и в одно текстовое поле они не лягут.
--
-- Ключ — ХЕШ формата без поставщика, ровно как у `sheets_for`/`set_sheets_by_signature`:
-- скелеты двух поставщиков могут совпасть, и тогда выбор у них общий. Решение то же, что
-- для листов, и расходиться с ним здесь нельзя.
CREATE TABLE IF NOT EXISTS signature_mark (
    signature  TEXT NOT NULL,              -- хеш формата
    brand      TEXT NOT NULL,              -- как написано в файле; показываем это
    brand_key  TEXT NOT NULL,              -- scope.normalize — по нему сверяем
    parse      INTEGER NOT NULL DEFAULT 0, -- флажок админа; 0 = не разбираем
    tm_code    TEXT,                       -- код марки в 1С (Справочники.Производители)
    tm_name    TEXT,                       -- имя марки — для показа
    discount   REAL,                       -- % скидки от розницы
    rows       INTEGER NOT NULL DEFAULT 0, -- строк в ПОСЛЕДНЕМ файле формата
    seen_at    TEXT NOT NULL,
    PRIMARY KEY (signature, brand_key)
);
CREATE INDEX IF NOT EXISTS ix_signature_mark ON signature_mark (signature);
"""


#: Типы доращиваемых колонок `supplier_signature`. Таблица старше их всех, и у работающей
#: базы колонок нет — без `ALTER TABLE` при старте все запросы к сигнатурам упали бы разом.
COLUMN_KINDS = {"sheets": "TEXT", "sheet_list": "TEXT",
                "brand_col": "INTEGER", "rate": "REAL",
                "currency_code": "TEXT", "currency_name": "TEXT"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Supplier:
    id: int
    name: str
    name_norm: str
    created_at: str


@dataclass(frozen=True)
class Signature:
    id: int
    supplier_id: int
    signature: str
    sample_name: str | None
    purpose: str | None
    first_seen: str
    last_seen: str
    #: Листы, которые разбирать, через запятую. Пусто — все.
    sheets: str = ""
    #: Все листы последнего файла этого формата — из чего выбирать.
    sheet_list: str = ""


@dataclass(frozen=True)
class SignatureMark:
    """Один бренд формата: что видно в файле и что решил админ.

    `rows` = 0 значит «в последнем файле этого бренда не было». Запись при этом остаётся,
    потому что вместе с ней остаются флажок, марка и скидка, — а поставщик вернёт бренд
    следующим файлом.
    """
    brand: str
    brand_key: str
    parse: bool = False
    tm_code: str = ""
    tm_name: str = ""
    discount: float | None = None
    rows: int = 0


@dataclass(frozen=True)
class PriceFile:
    id: int
    signature_id: int
    filename: str
    path: str
    received_at: str | None
    added_at: str


@dataclass(frozen=True)
class MergeResult:
    """Что сделало слияние — чтобы админу было что показать, а не «готово»."""
    moved: int          # сигнатур переехало к получателю
    absorbed: int       # сигнатур слилось с уже имевшимися у получателя
    files: int          # файлов переехало вместе с ними
    removed: bool       # уничтожен ли поставщик-источник


class SupplierStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            # Колонку завели позже самой таблицы: у работающей базы её нет, и без
            # дописывания все запросы к сигнатурам упали бы разом.
            cur = await db.execute("PRAGMA table_info(supplier_signature)")
            have = {row[1] for row in await cur.fetchall()}
            # `brand_col` — в какой колонке листа стоит бренд (находит код, помним у
            # формата); валюта с курсом — ПОСЛЕДНИЕ введённые, как подсказка новому прайсу
            # (сами они принадлежат прайсу: курс меняется каждый день).
            for column in ("sheets", "sheet_list", "brand_col", "rate",
                           "currency_code", "currency_name"):
                if have and column not in have:
                    await db.execute(
                        f"ALTER TABLE supplier_signature ADD COLUMN {column} "
                        + COLUMN_KINDS[column])
            await db.commit()

    # ------------------------------------------------------------ поставщики

    async def add_supplier(self, name: str) -> Supplier:
        """Найти по нормализованному имени либо завести.

        Идемпотентно намеренно: приём прайса зовёт это на каждом файле, и «уже есть» —
        нормальный исход, а не ошибка.
        """
        clean = (name or "").strip()
        if not clean:
            raise ValueError("имя поставщика не может быть пустым")
        norm = normalize(clean)

        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, name, name_norm, created_at FROM supplier WHERE name_norm = ?",
                (norm,))
            row = await cur.fetchone()
            if row:
                return Supplier(*row)

            cur = await db.execute(
                "INSERT INTO supplier (name, name_norm, created_at) VALUES (?, ?, ?)",
                (clean, norm, _now()))
            await db.commit()
            return Supplier(id=cur.lastrowid, name=clean, name_norm=norm,
                            created_at=_now())

    async def find_supplier(self, name: str) -> Supplier | None:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, name, name_norm, created_at FROM supplier WHERE name_norm = ?",
                (normalize(name or ""),))
            row = await cur.fetchone()
        return Supplier(*row) if row else None

    async def get_supplier(self, supplier_id: int) -> Supplier | None:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, name, name_norm, created_at FROM supplier WHERE id = ?",
                (supplier_id,))
            row = await cur.fetchone()
        return Supplier(*row) if row else None

    async def list_suppliers(self) -> list[Supplier]:
        """Сортировка по дате добавления — так админ видит список в спеке (§2.3)."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, name, name_norm, created_at FROM supplier ORDER BY created_at, id")
            return [Supplier(*row) for row in await cur.fetchall()]

    async def supplier_counts(self) -> dict[int, tuple[int, int]]:
        """{supplier_id: (сигнатур, файлов)} — одним запросом на весь список.

        Считаем здесь, а не в цикле по поставщикам: список показывается целиком, и запрос
        на каждого превратил бы вывод справочника в десятки обращений к базе.
        """
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT s.supplier_id, COUNT(DISTINCT s.id), COUNT(f.id) "
                "FROM supplier_signature AS s "
                "LEFT JOIN supplier_price_file AS f ON f.signature_id = s.id "
                "GROUP BY s.supplier_id")
            return {row[0]: (row[1], row[2]) for row in await cur.fetchall()}

    async def rename_supplier(self, supplier_id: int, name: str) -> bool:
        clean = (name or "").strip()
        if not clean:
            raise ValueError("имя поставщика не может быть пустым")
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE supplier SET name = ?, name_norm = ? WHERE id = ?",
                (clean, normalize(clean), supplier_id))
            await db.commit()
            return cur.rowcount > 0

    async def delete_supplier(self, supplier_id: int) -> bool:
        """Удалить ТОЛЬКО пустого поставщика.

        Непустого удалять нельзя: его сигнатуры и файлы остались бы сиротами, а прайсы —
        без владельца. Чтобы избавиться от дубля, для этого есть слияние (§2.3), и оно
        уничтожает источник само, когда у того не осталось прайсов.
        """
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM supplier_signature WHERE supplier_id = ?",
                (supplier_id,))
            if (await cur.fetchone())[0]:
                return False
            cur = await db.execute("DELETE FROM supplier WHERE id = ?", (supplier_id,))
            await db.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------- сигнатуры

    async def add_signature(self, supplier_id: int, signature: str,
                            sample_name: str | None = None,
                            purpose: str | None = None,
                            sheet_list: str | None = None) -> Signature:
        """Найти сигнатуру у ЭТОГО поставщика либо завести; отметить встречу.

        Повторная встреча той же сигнатуры — обычное дело (тот же прайс месяцем позже),
        поэтому обновляем `last_seen`, а не заводим вторую запись.
        """
        now = _now()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, supplier_id, signature, sample_name, purpose, first_seen, "
                "last_seen FROM supplier_signature WHERE supplier_id = ? AND signature = ?",
                (supplier_id, signature))
            row = await cur.fetchone()
            if row:
                await db.execute(
                    "UPDATE supplier_signature SET last_seen = ?, "
                    "sample_name = COALESCE(?, sample_name), "
                    "purpose = COALESCE(?, purpose), "
                    # Список листов ПЕРЕЗАПИСЫВАЕТСЯ свежим файлом, а не дополняется:
                    # поставщик добавляет и убирает листы, и накопленный список однажды
                    # предложил бы выбрать тот, которого в прайсе давно нет.
                    "sheet_list = COALESCE(?, sheet_list) WHERE id = ?",
                    (now, sample_name, purpose, sheet_list, row[0]))
                await db.commit()
                return Signature(row[0], row[1], row[2], sample_name or row[3],
                                 purpose or row[4], row[5], now)

            # Список листов пишется и ПРИ СОЗДАНИИ, а не только при повторной встрече:
            # первый файл формата — как раз тот, на котором листы и становятся известны,
            # и без этого выбирать их было бы не из чего до второго прайса.
            cur = await db.execute(
                "INSERT INTO supplier_signature (supplier_id, signature, sample_name, "
                "purpose, sheet_list, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (supplier_id, signature, sample_name, purpose,
                 (sheet_list or "").strip(), now, now))
            await db.commit()
            return Signature(cur.lastrowid, supplier_id, signature, sample_name,
                             purpose, now, now, "", (sheet_list or "").strip())

    async def find_signatures(self, signature: str) -> list[Signature]:
        """ВСЕ владельцы этой сигнатуры.

        Список, а не один: скелеты разных поставщиков могут совпасть (§2.2). Вызывающий
        обязан считаться с несколькими кандидатами, а не брать первого.
        """
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, supplier_id, signature, sample_name, purpose, first_seen, "
                "last_seen FROM supplier_signature WHERE signature = ? ORDER BY first_seen, id",
                (signature,))
            return [Signature(*row) for row in await cur.fetchall()]

    async def list_signatures(self, supplier_id: int | None = None) -> list[Signature]:
        where = " WHERE supplier_id = ?" if supplier_id is not None else ""
        params = (supplier_id,) if supplier_id is not None else ()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, supplier_id, signature, sample_name, purpose, first_seen, "
                "last_seen, COALESCE(sheets, ''), COALESCE(sheet_list, '') "
                "FROM supplier_signature"
                + where + " ORDER BY first_seen, id", params)
            return [Signature(*row) for row in await cur.fetchall()]

    async def set_signature_sheets(self, signature_id: int, sheets: str) -> bool:
        """Какие листы разбирать у этого формата. Пустая строка — снять ограничение.

        Храним КАК НАПИСАЛ АДМИН, без приведения к какому-то виду: имена листов сверяются
        с файлом нестрого (регистр и пробелы), а показывать надо то, что он задал, — иначе
        он не узнает в списке собственное указание.
        """
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE supplier_signature SET sheets = ? WHERE id = ?",
                ((sheets or "").strip(), signature_id))
            await db.commit()
            return cur.rowcount > 0

    async def set_sheets_by_signature(self, signature: str, sheets: str) -> bool:
        """То же, но адресуясь ХЕШОМ сигнатуры — так её знает форма 1С.

        Правим ВСЕ записи с этим хешом. Он может принадлежать двум поставщикам (скелеты
        совпали случайно), и выбрав одну запись, мы получили бы формат, который ведёт себя
        по-разному в зависимости от того, чей файл пришёл, — различить их админу было бы
        нечем, потому что в форме он видит один набор листов.
        """
        if not signature:
            return False
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE supplier_signature SET sheets = ? WHERE signature = ?",
                ((sheets or "").strip(), signature))
            await db.commit()
            return cur.rowcount > 0

    async def sheets_for(self, signature: str) -> str:
        """Листы по ХЕШУ сигнатуры — так её знает прайс на входе.

        Сигнатура может принадлежать двум поставщикам (скелеты совпали случайно), поэтому
        берём первое непустое указание: хоть одно заданное ограничение лучше, чем никакого,
        а разные указания на один скелет — повод для разбирательства, а не для тишины.
        """
        if not signature:
            return ""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT COALESCE(sheets, '') FROM supplier_signature "
                "WHERE signature = ? ORDER BY id", (signature,))
            for (value,) in await cur.fetchall():
                if (value or "").strip():
                    return value.strip()
        return ""

    # ----------------------------------------------------------------- бренды

    async def remember_marks(self, signature: str, found) -> int:
        """Запомнить состав брендов формата: `found` = [(бренд, строк, код ТМ, имя ТМ)].

        **РЕШЕНИЕ АДМИНА ПЕРЕЖИВАЕТ НОВЫЙ ФАЙЛ.** Приём зовёт это на каждом прайсе, и
        затирать флажок, марку или скидку было бы худшим из возможного: админ расставил их
        руками, а файл того же формата приходит каждый месяц.

        **НОВЫЙ БРЕНД ПРИЕЗЖАЕТ НЕОТМЕЧЕННЫМ.** Обратное умолчание значило бы, что
        появившийся в прайсе бренд молча уедет в разбор — за токены и с задачами по товарам,
        которых магазин может не возить.

        **ИСЧЕЗНУВШИЙ БРЕНД НЕ УДАЛЯЕМ, а ставим ему `rows = 0`.** Удалив, потеряли бы и
        флажок, и скидку, и привязку к марке, — а поставщик вернёт бренд следующим файлом.
        Ноль строк при этом ВИДЕН админу: «бренда больше нет в файле» это новость.

        Предложенный код марки пишется, ТОЛЬКО если своего ещё нет: догадка кода не вправе
        переписывать выбор человека.
        """
        now = _now()
        found = list(found or ())
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("UPDATE signature_mark SET rows = 0 WHERE signature = ?",
                             (signature,))
            for brand, rows, tm_code, tm_name in found:
                key = normalize(brand)
                if not key:
                    continue
                await db.execute(
                    "INSERT INTO signature_mark (signature, brand, brand_key, parse, "
                    "tm_code, tm_name, rows, seen_at) VALUES (?, ?, ?, 0, ?, ?, ?, ?) "
                    "ON CONFLICT (signature, brand_key) DO UPDATE SET "
                    "brand = excluded.brand, rows = excluded.rows, "
                    "seen_at = excluded.seen_at, "
                    "tm_code = CASE WHEN COALESCE(signature_mark.tm_code, '') = '' "
                    "    THEN excluded.tm_code ELSE signature_mark.tm_code END, "
                    "tm_name = CASE WHEN COALESCE(signature_mark.tm_code, '') = '' "
                    "    THEN excluded.tm_name ELSE signature_mark.tm_name END",
                    (signature, brand, key, tm_code or "", tm_name or "",
                     int(rows or 0), now))
            await db.commit()
        return len(found)

    async def marks_for(self, signature: str) -> list[SignatureMark]:
        """Бренды формата, в порядке файла (`rowid` — он же порядок первой вставки).

        Алфавитный был бы хуже: админ ищет бренд глазами там, где он стоит в книге, — та же
        причина, по которой листы в форме идут порядком вкладок.
        """
        if not signature:
            return []
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT brand, brand_key, parse, COALESCE(tm_code, ''), "
                "COALESCE(tm_name, ''), discount, rows FROM signature_mark "
                "WHERE signature = ? ORDER BY rowid", (signature,))
            return [SignatureMark(brand=row[0], brand_key=row[1], parse=bool(row[2]),
                                  tm_code=row[3], tm_name=row[4], discount=row[5],
                                  rows=row[6])
                    for row in await cur.fetchall()]

    async def marks_wanted(self, signature: str) -> list[str]:
        """Только отмеченные бренды — тем, кому нужен фильтр, а не вся таблица."""
        return [m.brand for m in await self.marks_for(signature) if m.parse]

    async def set_marks_by_signature(self, signature: str, rows) -> int:
        """Применить решение админа: `rows` = [{brand, parse, tm_code, tm_name, discount}].

        Адресуемся ХЕШОМ — так формат знает форма 1С. Бренды, которых в присланном наборе
        нет, НЕ ТРОГАЕМ: форма показывает состав последнего файла, и команда не обязана
        нести то, чего админ в ней не видел.
        """
        if not signature:
            return 0
        touched = 0
        async with aiosqlite.connect(self._db_path) as db:
            for row in rows or ():
                key = normalize(str(row.get("brand") or ""))
                if not key:
                    continue
                cur = await db.execute(
                    "UPDATE signature_mark SET parse = ?, tm_code = ?, tm_name = ?, "
                    "discount = ? WHERE signature = ? AND brand_key = ?",
                    (1 if row.get("parse") else 0,
                     str(row.get("tm_code") or ""), str(row.get("tm_name") or ""),
                     row.get("discount"), signature, key))
                touched += cur.rowcount
            await db.commit()
        return touched

    #: «Смотрели файл, колонки бренда в нём нет». Отличать это от «не смотрели вовсе»
    #: обязательно: иначе дозаполнение при старте перечитывало бы файлы форматов без
    #: брендов КАЖДЫЙ запуск — а их большинство (четыре формата из пяти, 04.10.2026).
    NO_BRAND_COLUMN = -1

    async def set_signature_brand_col(self, signature: str, column) -> bool:
        """В какой колонке листа стоит бренд. `NO_BRAND_COLUMN` — смотрели, её нет."""
        if not signature:
            return False
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE supplier_signature SET brand_col = ? WHERE signature = ?",
                (column, signature))
            await db.commit()
        return cur.rowcount > 0

    async def brand_col_for(self, signature: str):
        """Колонка бренда у формата либо None.

        Отметка «смотрели, колонки нет» отдаётся как None: для того, кто спрашивает про
        колонку, это один и тот же ответ, а отрицательный индекс, утёкший в разбор, резал
        бы лист по последней колонке.
        """
        value = await self._brand_col_raw(signature)
        return None if value is None or value < 0 else value

    async def brand_scanned(self, signature: str) -> bool:
        """Смотрели ли файл этого формата на бренды — включая исход «колонки нет».

        Нужно ровно дозаполнению при старте: оно обязано пройти каждый формат ОДИН раз, а
        не разбирать книгу заново при каждом перезапуске бота.
        """
        return await self._brand_col_raw(signature) is not None

    async def _brand_col_raw(self, signature: str):
        if not signature:
            return None
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT brand_col FROM supplier_signature "
                "WHERE signature = ? AND brand_col IS NOT NULL ORDER BY id", (signature,))
            row = await cur.fetchone()
        return int(row[0]) if row else None

    async def set_last_currency(self, signature: str, code: str = "", name: str = "",
                                rate=None) -> bool:
        """Последняя валюта и её курс у формата — ПОДСКАЗКА новому прайсу, а не его курс.

        Валюта одна на прайс и указывается человеком (решение админа 03.10.2026), курс
        относится именно к ней. Меняется курс каждый день, поэтому считать по прошлому
        нельзя: у прайса он свой. Здесь лежит только то, что подставить в форму.

        Код — по международному классификатору («978» EUR, «840» USD): по нему же 1С ищет
        элемент `Справочники.Валюты`, а имя хранится для показа.
        """
        if not signature:
            return False
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "UPDATE supplier_signature SET currency_code = ?, currency_name = ?, "
                "rate = ? WHERE signature = ?",
                (str(code or "").strip(), str(name or "").strip(), rate, signature))
            await db.commit()
        return cur.rowcount > 0

    async def last_currency(self, signature: str) -> dict:
        """Валюта и курс, введённые последними. Пустой код — считаем рублём, как раньше."""
        empty = {"code": "", "name": "", "rate": None}
        if not signature:
            return empty
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT COALESCE(currency_code, ''), COALESCE(currency_name, ''), rate "
                "FROM supplier_signature WHERE signature = ? ORDER BY id", (signature,))
            for code, name, rate in await cur.fetchall():
                if code or rate is not None:
                    return {"code": code, "name": name,
                            "rate": float(rate) if rate is not None else None}
        return empty

    async def move_signature(self, signature_id: int, supplier_id: int) -> bool:
        """Перепривязать сигнатуру к другому поставщику (§2.3).

        Если у получателя такая сигнатура уже есть — не плодим дубль, а переносим файлы в
        его запись и убираем лишнюю: пара (поставщик, сигнатура) обязана остаться одна.
        """
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT signature FROM supplier_signature WHERE id = ?", (signature_id,))
            row = await cur.fetchone()
            if not row:
                return False

            cur = await db.execute(
                "SELECT id FROM supplier_signature WHERE supplier_id = ? AND signature = ?",
                (supplier_id, row[0]))
            twin = await cur.fetchone()

            if twin and twin[0] != signature_id:
                await db.execute(
                    "UPDATE OR IGNORE supplier_price_file SET signature_id = ? "
                    "WHERE signature_id = ?", (twin[0], signature_id))
                await db.execute("DELETE FROM supplier_price_file WHERE signature_id = ?",
                                 (signature_id,))
                await db.execute("DELETE FROM supplier_signature WHERE id = ?",
                                 (signature_id,))
            else:
                await db.execute(
                    "UPDATE supplier_signature SET supplier_id = ? WHERE id = ?",
                    (supplier_id, signature_id))
            await db.commit()
            return True

    async def rehash_signature(self, signature_id: int, new: str) -> bool:
        """Переписать ХЕШ формата у одной записи, сохранив всё, что к ней привязано.

        **ЗАЧЕМ.** Правило подсчёта скелета изменилось 02.10.2026 — он перестал тащить в хеш
        данные файла. Формат остался тот же, значение хеша другое, и указание «разбирать
        такие-то листы» вместе с запомненными колонками осиротело бы молча: следующий прайс
        того же поставщика пришёл бы с новым хешом, не нашёл владельца и завёл ВТОРОГО
        поставщика с именем из имени файла. Ровно это и случилось, когда хеш разошёлся сам.

        Хеш у записи переписывается НА МЕСТЕ: номер записи не меняется, и всё, что ссылается
        на него (файлы прайсов, выбор листов), остаётся на своих местах.

        **Столкновение внутри одного поставщика сливается, а не падает.** Пара (поставщик,
        хеш) обязана остаться одной, а два старых формата вполне могли различаться только
        теми данными, которые новое правило больше не считает: тогда они и есть ОДИН формат.
        Выживает запись, у которой УЖЕ есть новый хеш; файлы переезжают к ней, а указание о
        листах подхватывается, если у неё самой его нет, — терять решение админа нельзя.
        """
        if not new:
            return False
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT supplier_id, signature, COALESCE(sheets, ''), "
                "COALESCE(sheet_list, '') FROM supplier_signature WHERE id = ?",
                (signature_id,))
            row = await cur.fetchone()
            if not row:
                return False
            supplier_id, old, sheets, sheet_list = row
            if old == new:
                return False

            cur = await db.execute(
                "SELECT id, COALESCE(sheets, ''), COALESCE(sheet_list, '') "
                "FROM supplier_signature WHERE supplier_id = ? AND signature = ?",
                (supplier_id, new))
            twin = await cur.fetchone()

            if twin and twin[0] != signature_id:
                await db.execute(
                    "UPDATE OR IGNORE supplier_price_file SET signature_id = ? "
                    "WHERE signature_id = ?", (twin[0], signature_id))
                await db.execute("DELETE FROM supplier_price_file WHERE signature_id = ?",
                                 (signature_id,))
                await db.execute(
                    "UPDATE supplier_signature SET sheets = ?, sheet_list = ? WHERE id = ?",
                    (twin[1] or sheets, twin[2] or sheet_list, twin[0]))
                await db.execute("DELETE FROM supplier_signature WHERE id = ?",
                                 (signature_id,))
            else:
                await db.execute(
                    "UPDATE supplier_signature SET signature = ? WHERE id = ?",
                    (new, signature_id))

            # БРЕНДЫ ПЕРЕЕЗЖАЮТ ВМЕСТЕ С ХЕШОМ. Их таблица ключуется ХЕШОМ, а не номером
            # записи, — и, оставшись под старым, выбор админа (флажки, марки, скидки)
            # осиротел бы молча: ровно так уже терялся выбор листов.
            #
            # OR REPLACE: под новым хешом бренды могли появиться раньше (два формата
            # свелись к одному), и тогда побеждают они — они посчитаны новым правилом.
            await db.execute(
                "UPDATE OR REPLACE signature_mark SET signature = ? WHERE signature = ?",
                (new, old))
            await db.commit()
            return True

    async def delete_signature(self, signature_id: int) -> bool:
        """Удалить сигнатуру вместе с записями о её файлах.

        Сами файлы с диска НЕ трогаем: за них отвечает `price_files.sweep`, и удалять их
        можно, только когда на них не ссылается ни один объект модели.
        """
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM supplier_price_file WHERE signature_id = ?",
                             (signature_id,))
            cur = await db.execute("DELETE FROM supplier_signature WHERE id = ?",
                                   (signature_id,))
            await db.commit()
            return cur.rowcount > 0

    # ----------------------------------------------------------- файлы прайсов

    async def add_price_file(self, signature_id: int, filename: str, path: str,
                             received_at: str | None = None) -> PriceFile:
        now = _now()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, signature_id, filename, path, received_at, added_at "
                "FROM supplier_price_file WHERE path = ?", (str(path),))
            row = await cur.fetchone()
            if row:
                return PriceFile(*row)

            cur = await db.execute(
                "INSERT INTO supplier_price_file (signature_id, filename, path, "
                "received_at, added_at) VALUES (?, ?, ?, ?, ?)",
                (signature_id, filename, str(path), received_at, now))
            await db.commit()
            return PriceFile(cur.lastrowid, signature_id, filename, str(path),
                             received_at, now)

    async def list_price_files(self, signature_id: int | None = None) -> list[PriceFile]:
        where = " WHERE signature_id = ?" if signature_id is not None else ""
        params = (signature_id,) if signature_id is not None else ()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT id, signature_id, filename, path, received_at, added_at "
                "FROM supplier_price_file" + where + " ORDER BY added_at, id", params)
            return [PriceFile(*row) for row in await cur.fetchall()]

    async def delete_price_file(self, file_id: int) -> str | None:
        """Убрать запись о файле. Возвращает путь — решать его судьбу не нам."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT path FROM supplier_price_file WHERE id = ?",
                                   (file_id,))
            row = await cur.fetchone()
            if not row:
                return None
            await db.execute("DELETE FROM supplier_price_file WHERE id = ?", (file_id,))
            await db.commit()
            return row[0]

    async def path_is_registered(self, path: str) -> bool:
        """Ссылается ли справочник на этот файл — для уборки сирот при старте."""
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT 1 FROM supplier_price_file WHERE path = ?", (str(path),))
            return await cur.fetchone() is not None

    async def known_paths(self) -> set[str]:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT path FROM supplier_price_file")
            return {row[0] for row in await cur.fetchall() if row[0]}

    # ---------------------------------------------------------------- слияние

    async def merge_suppliers(self, source_id: int, target_id: int) -> MergeResult:
        """Слить дубль в основного поставщика (§2.3).

        Сигнатуры вместе с подчинёнными файлами переезжают к получателю, источник
        уничтожается. Совпавшие сигнатуры не дублируются: их файлы вливаются в запись
        получателя.

        **Ссылки на поставщика из других таблиц переносятся ЗДЕСЬ ЖЕ** — это сбылось
        02.10.2026: `supplier_id` стоит у принятого прайса (`price`) и у журнала встреч
        артикулов (`price_sighting`), и без переноса слияние оставляло бы живой прайс со
        ссылкой на удалённого поставщика. Там, где поставщик лежит строкой (журнал цен,
        эксклюзивы), переносить по-прежнему нечего — в справочник они не мигрируют (§2.4).
        """
        if source_id == target_id:
            raise ValueError("нельзя слить поставщика с самим собой")

        moved = absorbed = files = 0
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT 1 FROM supplier WHERE id = ?", (target_id,))
            if not await cur.fetchone():
                raise ValueError(f"нет поставщика-получателя с id {target_id}")

            cur = await db.execute(
                "SELECT id, signature FROM supplier_signature WHERE supplier_id = ?",
                (source_id,))
            own = await cur.fetchall()

            for sig_id, sig in own:
                cur = await db.execute(
                    "SELECT id FROM supplier_signature "
                    "WHERE supplier_id = ? AND signature = ?", (target_id, sig))
                twin = await cur.fetchone()

                cur = await db.execute(
                    "SELECT COUNT(*) FROM supplier_price_file WHERE signature_id = ?",
                    (sig_id,))
                files += (await cur.fetchone())[0]

                if twin:
                    await db.execute(
                        "UPDATE OR IGNORE supplier_price_file SET signature_id = ? "
                        "WHERE signature_id = ?", (twin[0], sig_id))
                    await db.execute(
                        "DELETE FROM supplier_price_file WHERE signature_id = ?", (sig_id,))
                    await db.execute(
                        "DELETE FROM supplier_signature WHERE id = ?", (sig_id,))
                    absorbed += 1
                else:
                    await db.execute(
                        "UPDATE supplier_signature SET supplier_id = ? WHERE id = ?",
                        (target_id, sig_id))
                    moved += 1

            # ССЫЛКИ НА ПОСТАВЩИКА ПЕРЕНОСЯТСЯ ЗДЕСЬ ЖЕ — предупреждение выше сбылось
            # 02.10.2026. `supplier_id` давно стоит у принятого прайса и у журнала встреч
            # артикулов: слив дубль, мы оставили бы живой прайс со ссылкой на удалённого
            # поставщика. В форме 1С и в списке прайсов это пустое имя, а в журнале —
            # «есть у другого поставщика» про поставщика, которого нет.
            #
            # Таблицы живут в ЭТОМ ЖЕ файле базы (все хранилища делят `db_path`), но
            # заводят их другие модули, поэтому каждую проверяем на существование: в тестах
            # справочника их может не быть вовсе.
            for table, key in (("price", "supplier_id"),
                               ("price_sighting", "supplier_id")):
                cur = await db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,))
                if not await cur.fetchone():
                    continue
                # OR REPLACE: у журнала встреч поставщик входит в ключ, и у получателя уже
                # может стоять строка по тому же артикулу — тогда его запись и побеждает.
                await db.execute(
                    f"UPDATE OR REPLACE {table} SET {key} = ? WHERE {key} = ?",
                    (target_id, source_id))

            cur = await db.execute("DELETE FROM supplier WHERE id = ?", (source_id,))
            removed = cur.rowcount > 0
            await db.commit()

        return MergeResult(moved=moved, absorbed=absorbed, files=files, removed=removed)
