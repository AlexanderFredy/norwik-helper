"""Вид товара по названию из вопроса: «керамогранит» → код 1С `000000011`.

Нужен вопросу «где не хватает фото» (решение админа 11.10.2026): анализ идёт ТОЛЬКО по
названной категории, а 1С отбирает вид товара КОДОМ. Справочник видов берётся готовый —
`discontinued.MAPPING`, где у каждого вида товара с действующим ассортиментом уже стоит его
код; второй список однажды разошёлся бы с первым.

**Не угадываем.** Точное совпадение имени главнее вхождения («ламинат» — это «Ламинат», а не
«Виниловый ламинат»); два кандидата по вхождению или ни одного — это вопрос к человеку, и
ответ называет, из чего выбирать.
"""
from __future__ import annotations

from src.price_tool.discontinued import MAPPING


def _key(text) -> str:
    return " ".join(str(text or "").casefold().replace("ё", "е").split())


def known() -> list[tuple[str, str]]:
    """(название, код) всех видов товара, отсортированные по названию."""
    return sorted(((t.product_type, code) for code, t in MAPPING.items()),
                  key=lambda pair: pair[0])


def resolve(name: str | None) -> tuple[str, str]:
    """(код, название) вида товара. ValueError с текстом для ответа — если не вышло."""
    wanted = _key(name)
    options = ", ".join(title.lower() for title, _ in known())
    if not wanted:
        raise ValueError(f"Назови категорию товара — анализ по всему каталогу не делаю. "
                         f"Категории: {options}.")

    exact = [(code, title) for title, code in known() if _key(title) == wanted]
    if exact:
        return exact[0]

    # Вхождение в обе стороны: «плитка» → «Керамическая плитка», «межкомнатные двери» —
    # не вхождение, поэтому ещё и по словам: все слова вопроса есть в названии.
    words = set(wanted.split())
    loose = [(code, title) for title, code in known()
             if wanted in _key(title) or words <= set(_key(title).split())]
    if len(loose) == 1:
        return loose[0]
    if loose:
        raise ValueError(f"Категория «{name}» неоднозначна: "
                         + ", ".join(title.lower() for _, title in loose)
                         + ". Уточни, какую смотреть.")
    raise ValueError(f"Категории «{name}» нет. Категории: {options}.")
