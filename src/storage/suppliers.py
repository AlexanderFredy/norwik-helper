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
"""


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
                            purpose: str | None = None) -> Signature:
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
                    "purpose = COALESCE(?, purpose) WHERE id = ?",
                    (now, sample_name, purpose, row[0]))
                await db.commit()
                return Signature(row[0], row[1], row[2], sample_name or row[3],
                                 purpose or row[4], row[5], now)

            cur = await db.execute(
                "INSERT INTO supplier_signature (supplier_id, signature, sample_name, "
                "purpose, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                (supplier_id, signature, sample_name, purpose, now, now))
            await db.commit()
            return Signature(cur.lastrowid, supplier_id, signature, sample_name,
                             purpose, now, now)

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
                "last_seen FROM supplier_signature" + where + " ORDER BY first_seen, id",
                params)
            return [Signature(*row) for row in await cur.fetchall()]

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

        ВНИМАНИЕ на будущее: когда `supplier_id` появится в других таблицах (журнал цен,
        эксклюзивы), их ссылки надо переносить ЗДЕСЬ ЖЕ. Сейчас поставщик там лежит
        строкой и в справочник не мигрируется (§2.4), поэтому переносить нечего.
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

            cur = await db.execute("DELETE FROM supplier WHERE id = ?", (source_id,))
            removed = cur.rowcount > 0
            await db.commit()

        return MergeResult(moved=moved, absorbed=absorbed, files=files, removed=removed)
