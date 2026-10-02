"""Дозаполнить имена листов у форматов, которые завели до появления этой памяти.

**ЗАЧЕМ.** Список листов запоминается на ПРИЁМЕ прайса — только там он и известен. Но
форматы, заведённые раньше, остались без него, и форма выбора листов открывалась бы у них
пустой таблицей: выбирать не из чего, а почему — не видно (бой 02.10.2026, шесть прайсов).

**ПОЧЕМУ ЭТО МОЖНО ВОССТАНОВИТЬ.** Файлы прайсов лежат на диске, и сигнатура у формата как
раз от них и посчитана. Значит имена листов не потеряны — их достаточно прочитать.

**БЕРЁМ САМЫЙ СВЕЖИЙ ФАЙЛ формата**, а не первый: поставщик добавляет и убирает листы, и
список месячной давности предложил бы выбрать тот, которого в прайсе давно нет.

**ЗОВЁТСЯ ОДИН РАЗ ПРИ СТАРТЕ и ничего не перезаписывает**: формат, у которого список уже
есть, не трогается вовсе. Поэтому повторный запуск бесплатен, а свежий приём всегда главнее
этой починки.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Сколько форматов дозаполняем за один старт. Разбор книги — это чтение файла с диска,
#: секунды на крупном прайсе; упереться в сотню форматов на старте бота нельзя, а остаток
#: доберётся следующим запуском либо обычным приёмом.
LIMIT = 50


async def fill_sheet_lists(suppliers, limit: int = LIMIT) -> int:
    """Пройти форматы без списка листов и прочитать их из файлов. Возвращает, сколько
    заполнили."""
    from src.price_tool.parser import parse_price_table

    try:
        signatures = await suppliers.list_signatures()
    except Exception:                                   # noqa: BLE001
        logger.warning("Форматы прайсов не прочитались", exc_info=True)
        return 0

    filled = 0
    for sig in signatures:
        if filled >= limit:
            break
        if (sig.sheet_list or "").strip():
            continue

        files = await suppliers.list_price_files(sig.id)
        if not files:
            continue

        # Самый свежий: сортируем по дате получения, а при её отсутствии — по номеру записи.
        newest = max(files, key=lambda f: (f.received_at or "", f.id))
        path = Path(newest.path)
        if not path.is_file():
            continue

        try:
            sheets = parse_price_table(path.read_bytes(), newest.filename) or []
        except Exception:                               # noqa: BLE001
            logger.warning("Прайс %s не разобрался — листы формата %s не заполнены",
                           newest.filename, sig.signature[:12], exc_info=True)
            continue

        names = ", ".join(s.name for s in sheets)
        if not names:
            continue

        await suppliers.add_signature(sig.supplier_id, sig.signature, sheet_list=names)
        filled += 1
        logger.info("Формат %s: запомнил листы из %s — %s",
                    sig.signature[:12], newest.filename, names)

    return filled
