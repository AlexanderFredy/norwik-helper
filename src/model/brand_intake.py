"""Состав брендов формата и предложение марки 1С для каждого (решение админа 03.10.2026).

**ЗАЧЕМ ОТДЕЛЬНЫМ МОДУЛЕМ.** Список брендов нужен ДО первого обращения к модели: по нему
админ ставит флажки в форме 1С, и только потом идёт разбор. Собирать его — работа кода:
бренд стоит в колонке файла, решать тут нечего, а круг ручного цикла стоит $0.097.

**ПРЕДЛОЖЕНИЕ МАРКИ — ДОГАДКА, И ОНА ЗНАЕТ СВОИ ГРАНИЦЫ.** Неверная марка хуже
отсутствующей: по ней цены уехали бы ЧУЖОЙ ТМ, а на этом стоит выбор наименьшей цены между
поставщиками. Поэтому неоднозначное совпадение считается НЕ найденным, а последнее слово —
за админом (колонка «Марка» в форме). На боевом файле это и видно: из 23 ярлыков
«Остатков» в справочнике 1С нет тринадцати — ни Italon, ни линий Florim, ни Dogma.
"""
from __future__ import annotations

import logging

from src.price_tool.brands import brands_in, find_brand_column
from src.price_tool.parser import parse_price_table
from src.price_tool.scope import normalize

logger = logging.getLogger(__name__)


def _halves(name: str) -> list[str]:
    """Имя марки по частям: «Westerhof / Вестерхоф» — это два написания одной марки.

    В справочнике 1С двуязычная запись норма, и сравнивать поле целиком правильно везде,
    кроме сопоставления с чужим ярлыком: там совпадёт ровно одна половина.
    """
    return [normalize(part) for part in str(name or "").split("/") if normalize(part)]


def propose_marks(brands, marks) -> dict[str, tuple[str, str]]:
    """Бренд → (код марки, имя марки). Чего нет в ответе — то не опознано.

    Три шага по убыванию строгости, и каждый следующий включается, только когда предыдущий
    не дал НИ ОДНОГО попадания:

    1. имя целиком;
    2. половина двуязычного имени марки;
    3. первое слово ярлыка — «Italon Керамический гранит» и «Italon Настенная плитка» это
       два ярлыка одного бренда, и различает их вид товара, а не марка.

    **Двое и больше кандидатов — значит ни одного.** Выбор наугад между двумя марками
    однажды припишет цены не той.
    """
    index: dict[str, set[tuple[str, str]]] = {}
    for mark in marks or ():
        code = str(getattr(mark, "code", "") or "")
        name = str(getattr(mark, "name", "") or "")
        if not code or not name:
            continue
        for key in {normalize(name), *_halves(name)}:
            if key:
                index.setdefault(key, set()).add((code, name))

    out: dict[str, tuple[str, str]] = {}
    for brand in brands:
        words = normalize(brand).split()
        for probe in (normalize(brand), " ".join(words[:1])):
            if not probe:
                continue
            hits = index.get(probe) or set()
            if len(hits) == 1:
                out[brand] = next(iter(hits))
                break
    return out


def collect(content: bytes, filename: str):
    """Бренды файла: `(колонка, [(бренд, строк)])`. Колонка None — брендов в файле нет.

    Берутся ВСЕ листы, у которых колонка бренда есть: прайс бывает на несколько вкладок, и
    бренд с двух листов — это один бренд с суммой строк. Колонку запоминаем первую
    найденную — она нужна лишь для показа админу, а фильтр ищет её в каждом листе сам
    (разбор занимает двадцать строк и ничего не стоит).
    """
    try:
        sheets = parse_price_table(content, filename) or []
    except Exception:                                   # noqa: BLE001
        logger.warning("Бренды не собраны: %s не разобрался", filename, exc_info=True)
        return None, []

    column = None
    counts: dict[str, int] = {}
    order: list[str] = []
    for sheet in sheets:
        spot = find_brand_column(sheet)
        if spot is None:
            continue
        if column is None:
            column = spot.column
        for brand, rows in brands_in(sheet, spot):
            key = normalize(brand)
            if key not in counts:
                counts[key] = 0
                order.append(brand)
            counts[key] += rows

    return column, [(brand, counts[normalize(brand)]) for brand in order]


async def remember(suppliers, signature: str, content: bytes, filename: str,
                   marks=None) -> dict:
    """Собрать бренды, предложить марки и сложить в справочник. Возвращает сводку.

    Сводка — для сообщения админу: сколько брендов в файле, сколько отмечено к разбору,
    сколько осталось без марки в 1С. Молчать об этом нельзя: неотмеченный бренд в разбор не
    попадёт, и по форме это выглядит как «агент ничего не нашёл».

    1С недоступна — бренды всё равно запоминаются, просто без предложенных марок: список
    нужен сам по себе, а марку админ выставит в форме.
    """
    column, found = collect(content, filename)
    if not found:
        return {"brands": 0, "column": None, "wanted": 0, "without_tm": 0}

    guessed = propose_marks([brand for brand, _ in found], marks or [])
    await suppliers.remember_marks(
        signature,
        [(brand, rows, *guessed.get(brand, ("", ""))) for brand, rows in found])
    await suppliers.set_signature_brand_col(signature, column)

    rows = await suppliers.marks_for(signature)
    live = [m for m in rows if m.rows]
    return {"brands": len(live),
            "column": column,
            "wanted": sum(1 for m in live if m.parse),
            "without_tm": sum(1 for m in live if not m.tm_code)}
