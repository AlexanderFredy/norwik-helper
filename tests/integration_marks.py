"""Сухой прогон выбора брендов на настоящем файле: состав, марки, экономия.

Запуск:  python -m tests.integration_marks [путь к файлу] [бренд, бренд…]

Без аргументов берёт «Остатки 01.10.2026.xls» из `.claude/test-prices`. МОДЕЛЬ НЕ ЗОВЁТСЯ
вовсе — ни одного токена; 1С дёргается только за списком марок, и то необязательно (нет
настроек — просто не будет предложений).

Зачем отдельный прогон. Экономия на этой работе измеряется, а не предполагается: лист
разбирается страницами по 200 строк, и выигрыш — это РАЗНИЦА В ЧИСЛЕ СТРАНИЦ, которые
уедут модели. Юнит-тесты проверяют правила на синтетике, а здесь видно настоящий файл:
сколько в нём брендов, какие из них опознались в справочнике 1С и во что превращается
выбор.
"""
import sys
from pathlib import Path

from src.model.brand_intake import collect, propose_marks
from src.model.task_builder import MAX_SHEET_ROWS
from src.price_tool.brands import find_brand_column, only_brands
from src.price_tool.parser import non_empty_rows, parse_price_table, render_preview

DEFAULT = (Path(__file__).resolve().parent.parent.parent.parent
           / "test-prices" / "Остатки 01.10.2026.xls")

#: Тариф Opus 5 за миллион входных токенов и множитель записи в часовой кеш.
PER_MILLION = 5.0
CACHE_WRITE = 2.0


def pages(rows: int) -> int:
    """Сколько вызовов `read_price` понадобится: лист едет страницами."""
    return max(1, -(-rows // MAX_SHEET_ROWS))


def money(chars: int) -> str:
    tokens = chars / 4
    return (f"{int(tokens):>7} ток.  "
            f"${tokens / 1_000_000 * PER_MILLION * CACHE_WRITE:.3f}")


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    wanted = [a.strip() for a in sys.argv[2:] if a.strip()]
    if not path.is_file():
        print(f"Файл не найден: {path}")
        return 1

    content = path.read_bytes()
    print(f"Файл: {path.name}")

    sheets = parse_price_table(content, path.name) or []
    print(f"Листов: {len(sheets)}")

    column, found = collect(content, path.name)
    if not found:
        print("Колонка бренда не найдена — выбор брендов по этому формату недоступен, "
              "разбор идёт по листам, как обычно.")
        return 0

    print(f"Колонка бренда: {column}; брендов: {len(found)}; "
          f"строк с брендом: {sum(n for _, n in found)}")

    # Марки 1С — только если база настроена: прогон обязан работать и без неё.
    marks = []
    try:
        from src.config import load_config
        from src.onec.client import OnecClient

        config = load_config()
        if config.onec_base_url and config.onec_token:
            marks = OnecClient(config.onec_base_url, config.onec_token).selling_tm()
            print(f"Марок в справочнике 1С: {len(marks)}")
    except Exception as exc:                            # noqa: BLE001
        print(f"Марки 1С не прочитаны ({exc}) — предложений не будет")

    guessed = propose_marks([b for b, _ in found], marks)
    print(f"Опознано марок: {len(guessed)} из {len(found)}")
    print()
    for brand, rows in found:
        hit = guessed.get(brand)
        print(f"  {rows:>5}  {brand:<30} {hit[0] + '  ' + hit[1] if hit else '—'}")

    print()
    for sheet in sheets:
        spot = find_brand_column(sheet)
        if spot is None:
            continue
        rows = len(non_empty_rows(sheet)) - spot.header_rows
        whole = render_preview(sheet)
        print(f"Лист «{sheet.name}» целиком: {rows} строк, {pages(rows)} стр. чтения, "
              f"{money(len(whole))}")

        chosen = wanted or [brand for brand, _ in found[:2]]
        narrow = only_brands(sheet, spot, chosen)
        kept = len(non_empty_rows(narrow)) - spot.header_rows
        text = render_preview(narrow)
        print(f"  только {', '.join(chosen)}: {kept} строк, {pages(kept)} стр. чтения, "
              f"{money(len(text))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
