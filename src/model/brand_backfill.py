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

from src.price_tool.brand_rows import RULE_VERSION
from src.storage import price_files

logger = logging.getLogger(__name__)

#: Сколько форматов дозаполняем за один старт. Та же мерка, что у листов: разбор книги —
#: это чтение файла с диска, секунды на крупном прайсе, и упереться в сотню форматов на
#: старте бота нельзя. Остаток доберётся следующим запуском либо обычным приёмом.
LIMIT = 50


async def fill_brand_lists(suppliers, marks=None, logos=None,
                           limit: int = LIMIT) -> int:
    """Пройти форматы, у которых бренды ещё не смотрели, и прочитать их из файлов.

    `marks` — справочник марок 1С (`onec.selling_tm()`). Нужен и для предложения привязки, и
    САМОЙ детекции: разделитель, совпавший с именем настоящей марки, и бренд в имени листа
    опираются на него (так размечен прайс Стройиндустрии). `logos(сигнатура, содержимое, имя файла)` — чтение имён на баннерах
    (`logo_intake.name_logos`); без него формат с брендами-картинками просто не даст списка,
    и это лучше, чем выдумать его. Возвращает, сколько форматов заполнили.
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

        # ОТМЕТКА «СМОТРЕЛИ» ОТНОСИТСЯ К ПРАВИЛУ, КОТОРЫМ СМОТРЕЛИ (04.10.2026), и правило
        # НАЗЫВАЕТСЯ ВЕРСИЕЙ. Детекция за один день выросла дважды — с одной колонки до
        # пяти способов, — и оба раза уже просмотренные форматы остались бы с прежним
        # выводом: сперва у FLOOR SERVICE висел пустой способ, потом Стройиндустрия не
        # перечиталась новыми детекторами, потому что способ у неё уже стоял. Сравнение с
        # `RULE_VERSION` закрывает это раз и навсегда: поднял версию — форматы перечитаются
        # один раз, и больше никогда.
        if await suppliers.brand_scanned(sig.signature) \
                and await suppliers.brand_rule_for(sig.signature) >= RULE_VERSION:
            continue

        files = await suppliers.list_price_files(sig.id)
        if not files:
            continue

        newest = max(files, key=lambda f: (f.received_at or "", f.id))
        path = price_files.to_path(newest.path)     # путь мог прийти с Windows
        if not path.is_file():
            continue

        content = path.read_bytes()
        found_logos = {}
        if logos is not None:
            try:
                found_logos = await logos(sig.signature, content, newest.filename)
            except Exception:                           # noqa: BLE001
                logger.warning("Логотипы формата %s не прочитаны", sig.signature[:12],
                               exc_info=True)

        try:
            summary = await remember(suppliers, sig.signature, content,
                                     newest.filename, marks, found_logos)
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
            logger.info("Формат %s: бренд в %s не обозначен — выбор брендов недоступен",
                        sig.signature[:12], newest.filename)

    return filled
