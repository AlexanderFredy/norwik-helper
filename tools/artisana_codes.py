"""Соответствие «код Артисана (+…) → заводской код» из прайса — для обработки 1С.

Запуск:  python -m tools.artisana_codes "путь\\Price.xls" "путь\\artisana_codes.csv"

**ЗАЧЕМ.** Плитку Артисаны изначально заводили в 1С с ВНУТРЕННИМ кодом поставщика в поле
«Артикул» («+15433»). Такой код знает только Артисан: сверка с прайсом другого поставщика по
нему невозможна, а выбор наименьшей цены между поставщиками держится как раз на артикуле.
Решение админа (06.10.2026): код Артисана переезжает в реквизит «Арт», а «Артикул»
получает ЗАВОДСКОЙ код — тот, что стоит в прайсе отдельной колонкой.

**ПОЧЕМУ ЧИСТКУ ДЕЛАЕТ ЭТОТ СКРИПТ, А НЕ 1С.** В колонке «Заводской код» встречается
то, что артикулом не является: буквальная строка «нет кода» (у шести позиций), одно значение
дважды через перенос строки («44V179P\\r\\n44V179P»), код из одного символа. Записать такое в
«Артикул» значит сломать сверку на этих позициях молча. Парсер прайса здесь уже проверен на
этом самом файле, а чтение Excel средствами платформы — нет: у этой конфигурации трижды не
нашлось функций, которые должны были быть.

**ОБЩИЙ ЗАВОДСКОЙ КОД НЕ ВЫКИДЫВАЕТСЯ, А НАЗЫВАЕТСЯ.** У 34 заводских кодов в прайсе по
нескольку кодов Артисана: это разные позиции поставщика с одним кодом завода. Писать его
можно — сверка неуникальный артикул переживает (`артикул_не_различает_позиции`), — но
админ обязан видеть, где это случится: такой артикул не участвует в выборе наименьшей цены.
Число повторов едет в CSV отдельной колонкой, и обработка по умолчанию такие строки не
отмечает.

Формат CSV (UTF-8 с BOM, разделитель «;»): Код;Заводской;Повторов;Причина.
Пустой «Заводской» означает «писать нечего», и тогда «Причина» говорит почему.
"""
from __future__ import annotations

import csv
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

#: Значения, которыми поставщик заполняет ячейку, когда кода нет. Сравнение после
#: приведения к нижнему регистру и снятия пробелов по краям.
PLACEHOLDERS = frozenset({"нет кода", "нет", "без кода", "-", "—", "–", "n/a", "na",
                          "нет артикула", "?"})

#: Короче — это не код, а обрывок: «1» в колонке заводского кода прайса Артисаны.
MIN_LENGTH = 3

HEAD_CODE = "код"
HEAD_FACTORY = "заводской код"


@dataclass(frozen=True)
class Pair:
    code: str          # код Артисана, «+15433»
    factory: str       # заводской код; пусто — писать нечего
    reason: str = ""   # почему пусто


def clean_factory(raw) -> tuple[str, str]:
    """Заводской код и причина, если писать нечего. Пустая причина — код годен."""
    text = str(raw or "").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return "", "в прайсе нет заводского кода"

    # ОДНО ЗНАЧЕНИЕ ДВАЖДЫ ЧЕРЕЗ ПЕРЕНОС — это одно значение. Разные строки в одной ячейке
    # — уже два кода, и выбирать между ними код не вправе.
    if len(set(lines)) > 1:
        return "", "в ячейке несколько разных кодов: " + " / ".join(lines[:3])
    value = lines[0]

    if value.lower() in PLACEHOLDERS:
        return "", f"в прайсе вместо кода «{value}»"
    if len(value) < MIN_LENGTH:
        return "", f"слишком короткий заводской код «{value}»"
    return value, ""


def _header(rows) -> tuple[int, int, int] | None:
    """Строка заголовков и номера колонок «Код» и «Заводской код». None — не нашли."""
    for number, row in enumerate(rows[:40]):
        names = [" ".join(str(cell or "").split()).lower() for cell in row]
        if HEAD_CODE in names and HEAD_FACTORY in names:
            return number, names.index(HEAD_CODE), names.index(HEAD_FACTORY)
    return None


def pairs_from_sheet(rows) -> list[Pair]:
    """Все строки с кодом Артисана и чем заполнять «Артикул» у каждой."""
    found = _header(rows)
    if found is None:
        raise ValueError("не нашёл строку с колонками «Код» и «Заводской код»")
    head, code_col, factory_col = found

    out: list[Pair] = []
    for row in rows[head + 1:]:
        code = str(row[code_col] or "").strip() if len(row) > code_col else ""
        if not code.startswith("+"):
            continue
        raw = row[factory_col] if len(row) > factory_col else ""
        factory, reason = clean_factory(raw)
        out.append(Pair(code=code, factory=factory, reason=reason))
    return out


def shared_counts(pairs) -> Counter:
    """Сколько кодов Артисана у каждого заводского кода."""
    return Counter(p.factory for p in pairs if p.factory)


def write_csv(pairs, path: Path) -> None:
    shared = shared_counts(pairs)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["Код", "Заводской", "Повторов", "Причина"])
        for pair in pairs:
            writer.writerow([pair.code, pair.factory,
                             shared[pair.factory] if pair.factory else 0, pair.reason])


def main(argv=None) -> int:
    from src.price_tool.parser import parse_price_table

    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 2:
        print(__doc__.split("\n\n")[1])
        return 2
    source, target = Path(args[0]), Path(args[1])

    sheets = parse_price_table(source.read_bytes(), source.name) or []
    pairs: list[Pair] = []
    for sheet in sheets:
        try:
            pairs.extend(pairs_from_sheet(sheet.rows))
        except ValueError:
            continue
    if not pairs:
        print("В прайсе не нашлось ни одной строки с кодом «+» под колонкой «Код».")
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    write_csv(pairs, target)

    shared = shared_counts(pairs)
    good = [p for p in pairs if p.factory]
    print(f"Кодов Артисана: {len(pairs)}")
    print(f"  с заводским кодом:         {len(good)}")
    print(f"  из них с общим заводским:  {sum(1 for p in good if shared[p.factory] > 1)}")
    print(f"  писать нечего:             {len(pairs) - len(good)}")
    for reason, count in Counter(p.reason for p in pairs if p.reason).most_common():
        print(f"      {count:5} — {reason}")
    print(f"Записано: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
