"""«Когда последний раз меняли цены?» — ответы менеджеру (dev_tasks п.6).

Два источника, у каждого своя роль:

* **1С** — единственный авторитет по датам. `by-tm` отдаёт дату последнего изменения
  каждого вида цены, и она верна независимо от того, кто менял: агент или человек руками.
* **Наш журнал `price_writes`** — знает, из какого прайса цена взялась. Покрывает только
  записи, сделанные ботом; ручную правку в 1С он не видит и видеть не может.

Отсюда правило: дату берём из 1С всегда, источник — из журнала по совпадению даты, а если
записи нет, честно говорим «правили вручную», а не молчим и не выдумываем прайс.

Вся арифметика (преобладание даты >50%) считается здесь, а не моделью.
"""
from __future__ import annotations

from collections import Counter

from src.onec.client import NomItem

LABELS = {"purchase": "закуп", "rrc": "РРЦ", "retail": "наша роз."}
ORDER = ["purchase", "rrc", "retail"]

MANUAL = " Источник не наш — цену правили в 1С вручную."


def fmt_date(iso: str | None) -> str:
    if not iso or len(iso) < 10:
        return "?"
    y, m, d = iso[:4], iso[5:7], iso[8:10]
    return f"{d}.{m}.{y}"


def fmt_num(value) -> str:
    d = float(value)
    s = f"{d:,.0f}" if d == int(d) else f"{d:,.2f}"
    return s.replace(",", " ")


def _prices(item: NomItem) -> dict[str, tuple[float, str | None]]:
    """{вид цены: (значение, дата)} — только заданные в 1С."""
    out = {}
    for kind in ORDER:
        p = getattr(item, kind, None)
        if p:
            out[kind] = (p.value, p.date)
    return out


def _last_change(item: NomItem) -> str | None:
    dates = [d for _, d in _prices(item).values() if d]
    return max(dates) if dates else None


def dominant(dates: list[str | None]) -> tuple[str | None, int, int]:
    """Дата, на которую приходится БОЛЬШЕ половины изменений (п.6 ТЗ).

    Возвращает (дата или None, сколько на неё пришлось, всего с известной датой).
    Ровно 50% преобладанием не считаем — «выделить преобладание» нельзя.
    """
    known = [d for d in dates if d]
    if not known:
        return None, 0, 0
    date, count = Counter(known).most_common(1)[0]
    return (date if count * 2 > len(known) else None), count, len(known)


def _source(ref: str | None, date: str | None, sources: dict) -> str:
    """Хвост «Прайс «X» от dd.mm.yyyy» либо признание, что источник не наш."""
    if not date:
        return ""
    entry = sources.get((ref, date)) or sources.get((None, date))
    if not entry:
        return MANUAL
    doc = entry.get("price_doc") or entry.get("supplier")
    if not doc:
        return MANUAL
    when = f" от {fmt_date(entry['price_date'])}" if entry.get("price_date") else ""
    return f" Прайс «{doc}»{when}."


def _body(prices: dict[str, tuple[float, str | None]], with_dates: bool) -> str:
    if with_dates:
        return ", ".join(f"{LABELS[k]} {fmt_num(v)} от {fmt_date(d)}"
                         for k, (v, d) in prices.items())
    return ", ".join(f"{LABELS[k]} {fmt_num(v)}" for k, (v, _) in prices.items())


def describe_product(item: NomItem, sources: dict) -> str:
    """Точечный товар: «закуп 1050, РРЦ 1550, наша роз. 1200 от 20.07.2026. Прайс …»."""
    prices = _prices(item)
    if not prices:
        return f"{item.name}: цены в 1С не заданы."

    dates = {d for _, d in prices.values()}
    if len(dates) == 1:
        date = next(iter(dates))
        tail = f" от {fmt_date(date)}." if date else "."
        return f"{item.name}: {_body(prices, False)}{tail}{_source(item.ref, date, sources)}"

    # разные виды цен менялись в разные дни — дата у каждого, источники по всем датам
    seen, tails = set(), []
    for _, date in prices.values():
        src = _source(item.ref, date, sources)
        if src and src not in seen:
            seen.add(src)
            tails.append(src)
    return f"{item.name}: {_body(prices, True)}." + "".join(tails)


def describe_group(title: str, items: list[NomItem], sources: dict) -> str:
    """Коллекция или ТМ целиком — без перечисления товаров (п.6 ТЗ)."""
    priced = [i for i in items if _prices(i)]
    if not priced:
        return f"{title}: цены в 1С не заданы."

    n = len(priced)
    date_maps = {tuple(sorted((k, d) for k, (_, d) in _prices(i).items())) for i in priced}
    value_maps = {tuple(sorted((k, v) for k, (v, _) in _prices(i).items())) for i in priced}
    ref = priced[0].ref

    if len(date_maps) == 1:
        prices = _prices(priced[0])
        dates = {d for _, d in prices.values()}
        one_date = next(iter(dates)) if len(dates) == 1 else None
        if len(value_maps) == 1:
            # вся коллекция стоит одинаково и менялась одновременно — как товар
            if one_date:
                return (f"{title} ({n} поз.): {_body(prices, False)} от {fmt_date(one_date)}."
                        f"{_source(ref, one_date, sources)}")
            return f"{title} ({n} поз.): {_body(prices, True)}."
        if one_date:
            return (f"{title} ({n} поз.): цены менялись {fmt_date(one_date)}, "
                    f"значения у товаров разные.{_source(ref, one_date, sources)}")

    date, count, total = dominant([_last_change(i) for i in priced])
    if date:
        rest = total - count
        tail = f" У остальных ({rest} поз.) — в другие даты." if rest else ""
        src_ref = next((i.ref for i in priced if _last_change(i) == date), None)
        return (f"{title}: у {count} из {total} товаров цены менялись {fmt_date(date)}."
                f"{_source(src_ref, date, sources)}{tail}")
    return f"{title}: цены на разные товары менялись в разное время, посмотри в 1С."
