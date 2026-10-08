"""Дата прайса для уже принятых файлов — из шапки листа (бой 08.10.2026).

Прежде дата бралась только из имени файла, и «Price.xls» Артисаны жил без даты, хотя в
шапке стоит «Прайс-лист на 26.08.2026». Без даты его цены в выборе наименьшей между
поставщиками не старели НИКОГДА: недатированное предложение считается свежим
(`offers._comparable`), и через полгода оно по-прежнему перебивало бы свежие прайсы.

Приём теперь читает шапку сам (`intake.submit`); здесь — то же для принятых раньше, и
вместе с прайсом датируются его строки в журнале встреч: именно по ним выбирается цена.
Идемпотентно: трогаются только прайсы без даты, поэтому зовётся при каждом старте. Файл
не нашёлся или даты в шапке нет — прайс остаётся как был: выдумывать дату нельзя.
"""
from __future__ import annotations

import logging

from src.model.intake import read_signature
from src.price_tool.freshness import date_from_sheets
from src.storage import price_files

logger = logging.getLogger(__name__)


async def backfill(model_store, sightings=None) -> list[tuple[int, str]]:
    """Дописать даты. Возвращает (номер прайса, дата) того, что дописано."""
    done = []
    for price_id, path, filename, supplier_id, signature in await model_store.undated_prices():
        content = price_files.load(path)
        if not content:
            continue
        _, sheets = read_signature(content, filename or "price.xlsx")
        found = date_from_sheets(sheets)
        if not found or not await model_store.set_price_date(price_id, found):
            continue
        logger.info("Прайс №%s: дата %s взята из шапки листа", price_id, found)
        if sightings is not None:
            await sightings.date_undated(supplier_id, signature, found)
        done.append((price_id, found))
    return done
