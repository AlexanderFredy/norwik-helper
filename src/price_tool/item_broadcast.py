"""Сообщения о правках справочника: менеджерам и админу (§19.9 спеки).

Брат `broadcast.py`, который делает то же для цен, и устроен так же: сворачиваем по
коллекции, а не перечисляем позиции. Разбор прайса трогает десятки строк за раз, и список
из полусотни одинаковых правок читать не будет никто.

ДВА АДРЕСАТА, РАЗНЫЙ СОСТАВ
---------------------------
**Менеджеру** — только то, что меняет его разговор с покупателем: папка, коллекция,
коэффициент упаковки, цены, размеры. Про нормализацию не говорим ВООБЩЕ: двойной пробел в
наименовании его работы не касается.

**Админу** — то же самое плюс одна строка про нормализацию на коллекцию, без перечисления
позиций. Ему важно знать, что чистка была, а не какие именно пробелы ушли: на одной марке
Peli нормализация задела 49 позиций из 68 (§19.5), и подробности по каждой похоронили бы
четыре содержательных расхождения.

ОДИНАКОВОЕ ПО ВСЕЙ КОЛЛЕКЦИИ — ОДНОЙ СТРОКОЙ
--------------------------------------------
Если правка одна и та же у всех задетых позиций, пишем переход один раз. Если значения
разъехались — «у N поз.», как в ценовом broadcast: перечислять их всё равно бессмысленно,
а число показывает масштаб.
"""
from __future__ import annotations

from src.price_tool.history import LABELS, ORDER, fmt_num

# Что вообще сообщаем. Порядок фиксирован: сначала перемещения, потом числа — так строка
# читается от крупного к мелкому.
FIELDS = (
    ("parent", "папка"),
    ("collection", "коллекция"),
    ("pack", "коэффициент упаковки"),
    ("size", "размер"),
)


def _value(v) -> str:
    if v is None or v == "":
        return "нет"
    return fmt_num(v) if isinstance(v, (int, float)) else str(v)


def _transition(pairs: list[tuple]) -> str:
    """«157 → 190», «→ 190» (старые разные) или «у 5 поз.» (разное и то и то)."""
    olds = {p[0] for p in pairs}
    news = {p[1] for p in pairs}
    if len(news) == 1:
        new = _value(next(iter(news)))
        if len(olds) == 1:
            return f"{_value(next(iter(olds)))} → {new}"
        return f"→ {new}"
    return f"у {len(pairs)} поз."


def _scope(touched: int, total: int | None) -> str:
    """«вся коллекция» — только когда задеты действительно все её позиции."""
    if total and touched >= total:
        return "вся коллекция"
    return f"{touched} поз."


def _collection_line(group: dict, for_admin: bool) -> str | None:
    items = group.get("items") or []
    total = group.get("total")

    parts: list[str] = []

    for key, label in FIELDS:
        pairs = [tuple(i["changes"][key]) for i in items
                 if (i.get("changes") or {}).get(key)]
        if pairs:
            parts.append(f"{label} {_transition(pairs)} ({_scope(len(pairs), total)})")

    # Цены — теми же словами, что в ценовом broadcast: закупка, РРЦ, розница.
    by_kind: dict[str, list[tuple]] = {}
    for i in items:
        for kind, pair in ((i.get("changes") or {}).get("prices") or {}).items():
            by_kind.setdefault(kind, []).append(tuple(pair))
    for kind in ORDER:
        if kind in by_kind:
            parts.append(f"{LABELS[kind]} {_transition(by_kind[kind])} "
                         f"({_scope(len(by_kind[kind]), total)})")

    # Нормализация — ТОЛЬКО админу и ТОЛЬКО фактом, без разбора по позициям.
    if for_admin:
        normalized = sum(1 for i in items if i.get("normalized"))
        if normalized:
            parts.append(f"нормализация ({_scope(normalized, total)})")

    if not parts:
        return None

    return f"  • {group.get('collection') or '?'} — " + "; ".join(parts)


def build_item_broadcast(digest: dict, for_admin: bool = False) -> str | None:
    """Текст сообщения. None — сообщать нечего.

    Для менеджера коллекция, где была только нормализация, исчезает целиком: строки по ней
    не будет, и в заголовок марка не попадёт.
    """
    by_tm: dict[str, list[str]] = {}

    for group in digest.get("groups") or []:
        line = _collection_line(group, for_admin)
        if not line:
            continue
        tm = group.get("tm_name") or group.get("tm_code") or "?"
        by_tm.setdefault(tm, []).append(line)

    if not by_tm:
        return None

    head = "Правки в справочнике 1С"
    supplier = digest.get("supplier")
    if supplier:
        head += f" по прайсу {supplier}"

    lines = [head + ":"]
    for tm, rows in by_tm.items():
        lines.append(f"— {tm}")
        lines.extend(rows)

    return "\n".join(lines)
