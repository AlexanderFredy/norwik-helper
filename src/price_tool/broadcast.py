"""Короткое сообщение менеджерам об изменении цен и строки журнала (dev_tasks п.6).

Оба берутся из одного дайджеста — снимка того, что реально ушло в 1С, сохранённого рядом
с payload в `pending_proposal`. Дайджест, а не ответ 1С, потому что «было» знаем только мы:
1С в ответе возвращает старое значение не по всем формам запроса, а менеджеру интересен
именно переход.

Позиции, по которым 1С вернула ошибку, исключаются: сообщать об изменении цены, которая не
записалась, хуже, чем не сообщить.
"""
from __future__ import annotations

from src.price_tool.history import LABELS, ORDER, fmt_date, fmt_num


def _transition(rows: list[tuple[float | None, float]]) -> str | None:
    """«980 → 1050», «→ 1050» (старые разные) или «у 5 поз.» (разное и то и то)."""
    olds = {r[0] for r in rows}
    news = {r[1] for r in rows}
    if len(news) == 1:
        new = fmt_num(next(iter(news)))
        if len(olds) == 1:
            old = next(iter(olds))
            return f"{'нет' if old is None else fmt_num(old)} → {new}"
        return f"→ {new}"
    return f"у {len(rows)} поз."


def _collection_line(group: dict, skip: set[str]) -> str | None:
    items = [i for i in group.get("items", []) if i.get("ref") not in skip]
    by_kind: dict[str, list[tuple[float | None, float]]] = {}
    for item in items:
        for kind, pair in (item.get("prices") or {}).items():
            by_kind.setdefault(kind, []).append((pair[0], pair[1]))
    if not by_kind:
        return None
    parts = []
    for kind in ORDER:
        if kind in by_kind:
            parts.append(f"{LABELS[kind]} {_transition(by_kind[kind])}")
    touched = sum(1 for i in items if i.get("prices"))
    return f"- {group.get('collection') or '?'} — {touched} товаров ({', '.join(parts)})"


def build_broadcast(digest: dict, failed_refs: set[str] | None = None) -> str | None:
    """Сообщение менеджерам. None — если после отсева ошибок сообщать не о чем."""
    skip = failed_refs or set()
    by_tm: dict[str, list[str]] = {}
    counts: dict[str, int] = {}

    for group in digest.get("groups", []):
        line = _collection_line(group, skip)
        if not line:
            continue
        tm = group.get("tm_name") or group.get("tm_code") or "?"
        by_tm.setdefault(tm, []).append(line)
        counts[tm] = counts.get(tm, 0) + sum(
            1 for i in group.get("items", [])
            if i.get("prices") and i.get("ref") not in skip)

    if not by_tm:
        return None

    blocks = []
    for tm, lines in by_tm.items():
        blocks.append(f"Поменял цены на {tm} ({counts[tm]} товаров):\n" + "\n".join(lines))

    text = "\n\n".join(blocks)
    doc = digest.get("price_doc") or digest.get("supplier")
    if doc:
        when = f" от {fmt_date(digest['price_date'])}" if digest.get("price_date") else ""
        text += f"\n\nЦены брал из прайса «{doc}»{when}"
    return text


def journal_rows(digest: dict, written_on: str, failed_refs: set[str] | None = None) -> list[dict]:
    """Строки `price_writes`: по одной на товар и вид цены."""
    skip = failed_refs or set()
    rows = []
    for group in digest.get("groups", []):
        for item in group.get("items", []):
            if item.get("ref") in skip:
                continue
            for kind, pair in (item.get("prices") or {}).items():
                rows.append({
                    "written_on": written_on,
                    "tm_code": group.get("tm_code"), "tm_name": group.get("tm_name"),
                    "collection_ref": group.get("collection_ref"),
                    "collection": group.get("collection"),
                    "item_ref": item.get("ref"), "item_name": item.get("name"),
                    "price_type": kind, "old_value": pair[0], "new_value": pair[1],
                    "supplier": digest.get("supplier"), "price_doc": digest.get("price_doc"),
                    "price_date": digest.get("price_date"),
                })
    return rows
