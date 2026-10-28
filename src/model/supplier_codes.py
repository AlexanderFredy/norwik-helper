"""Внутренний код поставщика → реквизит «Арт» (решение админа 28.10.2026, Артисана).

В прайсе Артисаны две колонки кода: «Заводской код» — он лежит в «Артикул» 1С (решение
06.10.2026), — и «Код», внутренний артикул самой Артисаны, всегда на «+» («+37687»). Его
место — реквизит «Арт». Пишет его КОД, а не модель: при добавлении, изменении свойств и
нормализации значение берётся из строки прайса с тем же заводским артикулом. Модель
перепутала бы колонки (их две, обе «код»), а промах здесь тихий: «Арт» никто не читает
глазами, пока не понадобится сверка с поставщиком.

**ТОЛЬКО ПРАЙС С КОЛОНКОЙ «Код» НА «+».** Признак узкий намеренно: колонка с заголовком
ровно «Код», и больше половины её значений начинаются с «+». У других поставщиков такой
нет, и правило к ним не применяется вовсе.
"""
from __future__ import annotations

from src.model.refs import norm_article

#: Заголовок колонки внутреннего кода.
CODE_HEAD = "код"


def plus_codes(sheets) -> dict[str, str]:
    """Заводской артикул (нормализованный) → внутренний код на «+». Пусто — не тот прайс."""
    from src.model import price_check

    out: dict[str, str] = {}
    for sheet in sheets or ():
        rows = list(getattr(sheet, "rows", ()) or ())
        spec = price_check.guess_columns(rows) or {}
        article = spec.get("article")
        if not isinstance(article, int):
            continue
        article -= 1
        code = _code_column(rows)
        if code is None or code == article:
            continue

        pairs, plus, filled = {}, 0, 0
        for row in rows:
            value = str(_cell(row, code) or "").strip()
            if not value:
                continue
            filled += 1
            if not value.startswith("+"):
                continue
            plus += 1
            key = norm_article(str(_cell(row, article) or ""))
            if len(key) >= 3:
                pairs.setdefault(key, value)
        # Больше половины значений колонки — на «+»: значит, это та самая колонка.
        if filled and plus * 2 > filled:
            for key, value in pairs.items():
                out.setdefault(key, value)
    return out


def _cell(row, index: int):
    return row[index] if index < len(row) else None


def _code_column(rows) -> int | None:
    """Колонка с заголовком РОВНО «Код» в шапке (первые строки до первой с данными)."""
    for row in rows[:40]:
        cells = [" ".join(str(c or "").split()).casefold() for c in row]
        hits = [i for i, c in enumerate(cells) if c == CODE_HEAD]
        if len(hits) == 1 and sum(1 for c in cells if c) >= 3:
            return hits[0]
    return None


def fill_art(items: list[dict], codes: dict[str, str], cards: dict | None = None) -> int:
    """Дописать `art` в правки позиций, где его нет. Артикул — из правки, а нет его —
    из карточки 1С (`cards`: код 1С → карточка). Возвращает, скольким дописали."""
    if not codes:
        return 0
    filled = 0
    for raw in items:
        if str(raw.get("art") or "").strip():
            continue
        article = str(raw.get("article") or "").strip()
        if not article and cards is not None:
            card = cards.get(str(raw.get("ref") or "").strip())
            article = str(getattr(card, "article", "") or "")
        code = codes.get(norm_article(article))
        if code:
            raw["art"] = code
            filled += 1
    return filled
