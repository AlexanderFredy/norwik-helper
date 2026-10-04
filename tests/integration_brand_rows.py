"""Сухой прогон детекции брендов: что видит код в боевых прайсах. Без 1С и без модели.

Запуск: `python -m tests.integration_brand_rows`

Печатает по каждому файлу способ детекции, список брендов с числом строк и ценой выборки в
токенах. Он же доказывает экономию: у Артисаны два ненужных магазину бренда — 399 токенов
против 316 440 за весь файл.

**ГЛАВНОЕ, ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ, — МОЛЧАНИЕ.** На мелких форматах правило «одинокая ячейка
= раздел» даёт мусор (у Most Floor так выглядят товары, у Линдервуда — примечание), и
детектор обязан не находить ничего, а не выдавать список из товарных строк.
"""
from __future__ import annotations

import sys
from pathlib import Path

from src.price_tool.brand_rows import (brand_map, brands_in_rows, only_brand_rows,
                                       rows_cost)
from src.price_tool.parser import non_empty_rows, parse_price_table

PRICES = Path(r"C:\Data\ClodeCodeProjects\shop-helper\.claude\test-prices")
STORED = Path("data/prices")


def look(path: Path, picks=()) -> None:
    try:
        content = path.read_bytes()
    except OSError as exc:
        print(f"{path.name}: не прочитался — {exc}")
        return

    print(f"\n=== {path.name}")
    for sheet in parse_price_table(content, path.name) or []:
        rows = non_empty_rows(sheet)
        chars = sum(len("\t".join(str(c or "") for c in r)) for r in rows)
        spot = brand_map(sheet)
        if spot is None:
            print(f"  [{sheet.name}] строк {len(rows):6}, ~{chars // 4:7} токенов — "
                  f"бренды не обозначены")
            continue

        found = brands_in_rows(spot)
        print(f"  [{sheet.name}] строк {len(rows):6}, ~{chars // 4:7} токенов — "
              f"способ: {spot.mode}, шапка {spot.header_rows}, брендов {len(found)}")
        for brand, lines in sorted(found, key=lambda p: -p[1])[:12]:
            picked, cost = rows_cost(sheet, spot, [brand])
            print(f"      {brand[:42]:42} {lines:6} строк  ~{cost // 4:7} токенов")
        if len(found) > 12:
            print(f"      … ещё {len(found) - 12} брендов")

        for pick in picks:
            lines, cost = rows_cost(sheet, spot, pick)
            kept = only_brand_rows(sheet, spot, pick)
            print(f"      ВЫБОР {pick}: {lines} строк, ~{cost // 4} токенов, "
                  f"в листе {len(kept.rows)} строк "
                  f"({cost * 100 // max(chars, 1)}% файла)")


def main() -> None:
    look(PRICES / "Price.xls",
         picks=[["APE", "Amadis Fine Tiles, S.A."], ["Atlas Concorde ( Italy)"]])
    look(PRICES / "Остатки 01.10.2026.xls", picks=[["ABK", "VitrA"]])
    for name in ("Прайс лист Most Floor.xlsx",
                 "Прайс (от 28.08.2026) на  ламинат Westerhof  ПОД ЗАКАЗ  МРЦ.xls"):
        look(PRICES / name)
    for path in sorted(STORED.glob("*.xls*")):
        look(path)


if __name__ == "__main__":
    sys.exit(main())
