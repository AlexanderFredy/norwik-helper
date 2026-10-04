"""Дозаполнить бренды у форматов, которые завели до появления этой памяти.

**ЗАЧЕМ.** Состав брендов собирается на ПРИЁМЕ прайса и при пересборке задач — только там
файл в руках. Форматы, заведённые раньше, остались без него, и форма выбора открывалась у
них с пустым правым списком: выбирать не из чего, а почему — не видно (вопрос админа
04.10.2026, пять форматов, `signature_mark` пуст целиком). Ровно та же поломка, что была у
листов двумя днями раньше, и лечится она тем же способом — см. [[sheet_backfill]].

**ПОЧЕМУ ЭТО МОЖНО ВОССТАНОВИТЬ.** Файлы прайсов лежат на диске, бренд стоит в колонке
файла, а колонку находит код (`price_tool.brands`). Значит список не потерян — его
достаточно прочитать.

**БЕРЁМ САМЫЙ СВЕЖИЙ ФАЙЛ формата**, а не первый: поставщик добавляет и убирает бренды, и
состав месячной давности предложил бы отметить то, чего в прайсе давно нет.

**МАРКИ 1С — ПОДСКАЗКА, И БЕЗ 1С ОНА ПРОПУСКАЕТСЯ, А СПИСОК НЕТ.** Список брендов нужен сам
по себе: по нему админ ставит флажки. Марку он выставит в форме, а код лишь предлагает —
поэтому недоступная 1С не повод не заполнять список.

**ЗОВЁТСЯ ОДИН РАЗ ПРИ СТАРТЕ и ничего не перезаписывает**: формат, который уже смотрели,
не трогается вовсе — включая исход «колонки бренда в файле нет» (`brand_scanned`). Без этого
отличия четыре формата из пяти разбирались бы заново при каждом перезапуске бота. Флажки,
марки и скидки переживают дозаполнение по той же причине, по которой переживают новый файл:
их ставил человек (`remember_marks`).
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Сколько форматов дозаполняем за один старт. Та же мерка, что у листов: разбор книги —
#: это чтение файла с диска, секунды на крупном прайсе, и упереться в сотню форматов на
#: старте бота нельзя. Остаток доберётся следующим запуском либо обычным приёмом.
LIMIT = 50


async def fill_brand_lists(suppliers, marks=None, limit: int = LIMIT) -> int:
    """Пройти форматы, у которых бренды ещё не смотрели, и прочитать их из файлов.

    `marks` — справочник марок 1С (`onec.selling_tm()`), нужен только для предложения
    привязки. Возвращает, сколько форматов заполнили.
    """
    from src.model.brand_intake import remember

    try:
        signatures = await suppliers.list_signatures()
    except Exception:                                   # noqa: BLE001
        logger.warning("Форматы прайсов не прочитались", exc_info=True)
        return 0

    filled = 0
    seen: set[str] = set()
    for sig in signatures:
        if filled >= limit:
            break
        # Один хеш бывает у двух поставщиков (скелеты совпали случайно), а бренды лежат
        # по ХЕШУ — второй проход по тому же формату был бы разбором того же файла.
        if sig.signature in seen:
            continue
        seen.add(sig.signature)

        if await suppliers.brand_scanned(sig.signature):
            continue

        files = await suppliers.list_price_files(sig.id)
        if not files:
            continue

        newest = max(files, key=lambda f: (f.received_at or "", f.id))
        path = Path(newest.path)
        if not path.is_file():
            continue

        try:
            summary = await remember(suppliers, sig.signature, path.read_bytes(),
                                     newest.filename, marks)
        except Exception:                               # noqa: BLE001
            logger.warning("Прайс %s не разобрался — бренды формата %s не заполнены",
                           newest.filename, sig.signature[:12], exc_info=True)
            continue

        filled += 1
        if summary.get("brands"):
            logger.info("Формат %s: запомнил %s брендов из %s, без марки в 1С — %s",
                        sig.signature[:12], summary["brands"], newest.filename,
                        summary.get("without_tm"))
        else:
            logger.info("Формат %s: колонки бренда в %s нет — выбор брендов недоступен",
                        sig.signature[:12], newest.filename)

    return filled
