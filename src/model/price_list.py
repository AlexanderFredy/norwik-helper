"""Правила списка прайсов: свежесть и ссылки на более новый (§3.2).

Чистые функции над списком `Price` — ни базы, ни визуалов. Так их можно проверить на
десятке строк вместо поднятия бота.

**Ключ «тот же прайс» — пара (поставщик, сигнатура), а не поставщик.** Поставщик дробит
прайс по типам товаров, и по одному поставщику «плитка» оказалась бы новее «ламината».
"""
from __future__ import annotations

from src.model.price import Price
from src.price_tool.freshness import is_newer


def group_key(price: Price) -> tuple[int, str]:
    return (price.supplier_price.supplier_id, price.supplier_price.signature)


def newest_of(prices: list[Price]) -> Price | None:
    """Самый свежий из списка одной группы.

    Сравнение — существующая `freshness.is_newer`: сперва дата самого прайса, потом дата
    получения. При полном равенстве побеждает пришедший позже по порядку в списке: иначе
    результат зависел бы от того, как список отсортирован.
    """
    best: Price | None = None
    for price in prices:
        if best is None or is_newer(price.supplier_price.freshness(),
                                    best.supplier_price.freshness()):
            best = price
    return best


def relink(prices: list[Price]) -> int:
    """Пересчитать ссылки «на более свежий» во ВСЁМ списке. Возвращает число изменений.

    Считаем от нуля, а не правим по одной ссылке, и это осознанно: спека требует, чтобы
    ссылка всегда вела на САМЫЙ НОВЫЙ прайс, а при появлении ещё более нового — чтобы у
    всех устаревших она обновилась. Инкрементальная правка обязана угадать все затронутые
    записи; пересчёт не может их пропустить.

    Тем же вызовом закрывается и уничтожение самого свежего: ссылки устаревших
    переставляются на следующий по свежести из оставшихся, а очищаются только когда более
    новых не осталось вовсе.
    """
    groups: dict[tuple[int, str], list[Price]] = {}
    for price in prices:
        groups.setdefault(group_key(price), []).append(price)

    changed = 0
    for members in groups.values():
        newest = newest_of(members)
        for price in members:
            target = None if price is newest else (newest.id if newest else None)
            # Ссылка на себя бессмысленна: она означала бы «я устарел относительно себя».
            if target == price.id:
                target = None
            if price.newer_id != target:
                price.newer_id = target
                changed += 1
    return changed


def is_outdated(candidate: Price, prices: list[Price]) -> bool:
    """Есть ли в списке прайс той же группы СВЕЖЕЕ кандидата.

    Зовётся при приёме: обычный приём такой прайс отклоняет, принудительный — берёт и сразу
    помечает устаревшим (§4).
    """
    key = group_key(candidate)
    for price in prices:
        if price is candidate or group_key(price) != key:
            continue
        if is_newer(price.supplier_price.freshness(),
                    candidate.supplier_price.freshness()):
            return True
    return False


def same_group(prices: list[Price], candidate: Price) -> list[Price]:
    """Прайсы того же поставщика и того же формата."""
    key = group_key(candidate)
    return [p for p in prices if p is not candidate and group_key(p) == key]
