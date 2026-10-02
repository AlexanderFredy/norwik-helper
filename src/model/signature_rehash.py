"""Пересчитать хеши форматов прайсов по новому правилу скелета (правка 02.10.2026).

**ЗАЧЕМ.** `price_signature` перестал тащить в скелет данные файла — теперь это имена листов
и строка заголовков колонок, и только они (см. `src/price_tool/signature.py`). Формат от
этого не изменился, а ЗНАЧЕНИЕ хеша изменилось у всех. Хеш — ключ: на нём держится выбор
листов к разбору, запомненные колонки цен, журнал встреч артикулов и опознание владельца
формата. Оставь старые значения на месте — и следующий прайс каждого поставщика пришёл бы с
новым хешом, не нашёл владельца и завёл ВТОРОГО поставщика с именем из имени файла, обнулив
выбор листов. Именно так это и выглядело, когда хеш разошёлся сам собой.

**ПОЧЕМУ ЭТО МОЖНО ПЕРЕСЧИТАТЬ.** Файлы прайсов лежат на диске, и хеш у формата от них и
посчитан. Берём самый свежий файл формата, считаем новым правилом и переписываем значение
во ВСЕХ хранилищах, которые им ключуются.

**ИДЕМПОТЕНТНО.** На втором запуске пересчёт даёт то, что уже записано, и работа не делается
вовсе. Поэтому зовётся при каждом старте и ничего не стоит.

**ЧЕГО НЕ ДЕЛАЕМ.** Формат, чей файл не сохранился или не разобрался, не трогаем: гадать
нельзя, а своё старое значение он сохранит и дальше — потеряется он не молча, а заметно, при
следующем приёме. Хеш содержимого (`без-разбора:…`) не пересчитываем: это не скелет.

**ДУБЛЬ ПОСТАВЩИКА НЕ СЛИВАЕМ САМИ.** Пересчёт может свести к одному хешу форматы ДВУХ
поставщиков — ровно случай, из-за которого правка и появилась: один из них настоящий, второй
заведён по имени файла. Слияние справочника необратимо и адресует историю цен, поэтому это
решение админа (`/supplier_merge`), а наше дело — назвать находку вслух.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Сколько форматов пересчитываем за один старт. Разбор книги — это чтение файла с диска,
#: секунды на крупном прайсе; упираться в сотню форматов на старте бота нельзя, а остаток
#: доберётся следующим запуском.
LIMIT = 50

#: Приставка личности файла, который не разобрался. Скелета у него нет, пересчитывать нечего.
RAW_PREFIX = "без-разбора:"


async def rehash_signatures(suppliers, *, pricing=None, sightings=None, model_store=None,
                            limit: int = LIMIT) -> list[dict]:
    """Пересчитать хеши и перевесить всё, что на них ключуется.

    Возвращает список правок: `{"supplier", "old", "new", "file"}` — по строке на формат.
    Пустой список значит «пересчитывать было нечего», а не «не сработало».
    """
    from src.price_tool.parser import parse_price_table
    from src.price_tool.signature import price_signature

    try:
        signatures = await suppliers.list_signatures()
    except Exception:                                   # noqa: BLE001
        logger.warning("Форматы прайсов не прочитались", exc_info=True)
        return []

    done: list[dict] = []
    for sig in signatures:
        if len(done) >= limit:
            break
        if not sig.signature or sig.signature.startswith(RAW_PREFIX):
            continue

        files = await suppliers.list_price_files(sig.id)
        if not files:
            continue

        # Самый свежий файл: правило считает скелет по нему же при обычном приёме, и
        # пересчёт обязан получить то значение, которое получит следующий прайс.
        newest = max(files, key=lambda f: (f.received_at or "", f.id))
        path = Path(newest.path)
        if not path.is_file():
            continue

        try:
            sheets = parse_price_table(path.read_bytes(), newest.filename) or []
        except Exception:                               # noqa: BLE001
            logger.warning("Формат %s: файл %s не разобрался — хеш оставлен прежним",
                           sig.signature[:12], newest.filename, exc_info=True)
            continue

        fresh = price_signature(sheets) if sheets else ""
        if not fresh or fresh == sig.signature:
            continue

        supplier = await suppliers.get_supplier(sig.supplier_id)
        old = sig.signature

        # СПРАВОЧНИК ПЕРВЫМ: он решает судьбу записи (переписать хеш или слить с двойником).
        # Остальные хранилища знают только пару «было → стало», и порядок им безразличен.
        await suppliers.rehash_signature(sig.id, fresh)

        for store in (pricing, sightings, model_store):
            if store is None:
                continue
            try:
                await store.rehash_signature(old, fresh)
            except Exception:                           # noqa: BLE001
                # Одно не перевесившееся хранилище не повод бросать остальные: хуже всего
                # половина записей под старым хешом и половина под новым.
                logger.warning("Хеш формата %s → %s: %s не перевесилось",
                               old[:12], fresh[:12], type(store).__name__, exc_info=True)

        done.append({"supplier": supplier.name if supplier else "",
                     "old": old, "new": fresh, "file": newest.filename})

    await _warn_about_twins(suppliers, done)
    return done


async def _warn_about_twins(suppliers, done: list[dict]) -> None:
    """Назвать форматы, которые после пересчёта оказались у ДВУХ поставщиков.

    Это не сбой пересчёта, а находка: скорее всего один из поставщиков заведён по имени
    файла, когда хеш разошёлся. Сливать сами не имеем права — назовём, чтобы решение было
    у админа и чтобы следующий прайс этого формата не достался случайному владельцу.
    """
    for edit in done:
        try:
            owners = await suppliers.find_signatures(edit["new"])
        except Exception:                               # noqa: BLE001
            continue
        if len({o.supplier_id for o in owners}) < 2:
            continue
        names = []
        for owner in owners:
            supplier = await suppliers.get_supplier(owner.supplier_id)
            names.append(f"{owner.supplier_id} {supplier.name if supplier else '?'}")
        logger.warning(
            "Формат %s после пересчёта у нескольких поставщиков: %s — похоже на дубль, "
            "слить командой /supplier_merge", edit["new"][:12], "; ".join(names))
