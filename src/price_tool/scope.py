"""Категории товаров, которые вообще анализируем (§6.8 спеки).

Задаётся один раз на все прайсы, а не на каждый файл: у поставщиков в прайсе годами
лежат одни и те же лишние разделы — плинтус, подложка, аксессуары, стеновые панели, — и
отвечать про них каждый месяц заново бессмысленно.

Пустой список означает «ограничений нет», а не «не анализировать ничего». Иначе свежая
установка молча перестала бы обрабатывать прайсы, и понять почему было бы нечем.

Сопоставление нестрогое: в 1С вид товара называется «Керамическая плитка», а админ
напишет «плитка». Совпадением считается вхождение одной нормализованной строки в другую.
"""
from __future__ import annotations

import re

_SPACES = re.compile(r"\s+")
_PUNCT = re.compile(r"[^0-9a-zа-я ]+")


def normalize(name: str | None) -> str:
    """«Керамическая  плитка,» → «керамическая плитка»; ё→е, регистр и пунктуация снимаются."""
    text = (name or "").lower().replace("ё", "е")
    return _SPACES.sub(" ", _PUNCT.sub(" ", text)).strip()


def matches(category: str, product_type: str | None) -> bool:
    """Категория админа ↔ вид товара 1С.

    Вхождение в обе стороны: «плитка» покрывает «Керамическая плитка», а «Двери
    межкомнатные» из 1С покрывается коротким «двери».
    """
    a, b = normalize(category), normalize(product_type)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def in_scope(scope: list[str], product_type: str | None) -> bool:
    """Анализируем ли этот вид товара. Пустой scope пропускает всё."""
    if not scope:
        return True
    return any(matches(c, product_type) for c in scope)


def split(scope: list[str], product_types) -> tuple[list[str], list[str]]:
    """Виды товара → (которые смотрим, которые пропускаем). Порядок сохраняется."""
    watched, skipped = [], []
    for product_type in dict.fromkeys(t for t in product_types if t):
        (watched if in_scope(scope, product_type) else skipped).append(product_type)
    return watched, skipped


def describe(scope: list[str]) -> str:
    """Строка для промпта и отчётов."""
    if not scope:
        return ("Ограничений по категориям нет — админ их не задавал, обрабатывай все "
                "виды товара, какие найдёшь.")
    return ("Анализируем только эти категории товаров: " + ", ".join(scope)
            + ". Разделы прайса других категорий НЕ разбирай и НЕ сопоставляй — просто "
              "перечисли их одной короткой строкой в предупреждениях.")
