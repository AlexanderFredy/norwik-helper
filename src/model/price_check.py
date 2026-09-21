"""Сверка цен прайса с текущими ценами 1С при сборке задач (§6.1).

**Зачем.** Задача «изменение цен» заводилась по факту наличия ценовых колонок в прайсе:
есть колонки — есть задача. На Most Flooring / Millenium Pro это дало задачу на восемь
позиций, у которых закупка и РРЦ уже совпадали с прайсом. Прогон по такой задаче стоит
полного круга ручного цикла и кончается словами «менять нечего», а список, где половина
задач пустая, админ перестаёт читать.

**Почему это делает код, а не модель.** Сравнить два числа — не работа для рассуждений, а
текущие цены и так лежат в выгрузке `by-tm`, которую сборщик уже держит в памяти. От модели
нужно ровно одно, чего код не знает: КАКИЕ КОЛОНКИ листа считать артикулом, закупкой и РРЦ.
Она их и так читает глазами, поэтому называет один раз на лист (`price_columns`), а всё
остальное — вычитывание, приведение чисел, сравнение с порогом — делает этот модуль. Это
дешевле любого другого расклада: лишних обращений к 1С ноль, лишних кругов цикла ноль,
выходных токенов модели — одна строка на лист вместо цен по каждой позиции.

**Порог берётся готовый** — `SAME_PRICE_PCT` из `price_tool.changes`, тот же, которым
пользуется запись цен. Свой завёлся бы и разошёлся с ним молча.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from src.price_tool.changes import SAME_PRICE_PCT, same_price
from src.model.refs import norm_article

# Виды цен, которые сверяем. Розница сюда не входит намеренно: она считается по правилам
# сайта (`specs/retail-price-rules.md`), а не берётся из прайса.
#
# Это НЕ значит, что её пишут вслепую. При записи её держат те же 2%: закупка не изменилась
# — розница не пересчитывается вовсе (§8 правил, `retail.compute_retail`,
# `purchase_changed=False`); пересчиталась, но разошлась с текущей меньше чем на
# `MIN_CHANGE_PCT` — не пишется (`retail.py`, причина `below_threshold`). В `set-prices`
# уезжают только планы с непустым `prices`, так что лишних записей в 1С не возникает.
KINDS = ("purchase", "rrc")
LABEL = {"purchase": "закупка", "rrc": "РРЦ"}

# Мусор, который бывает в ценовой ячейке: валюта, единица, неразрывные пробелы.
_CLEAN = re.compile(r"[^\d,.\-]")


@dataclass(frozen=True)
class Columns:
    """Разрешённые номера колонок (с нуля). `article` обязателен, цены — нет."""
    article: int
    purchase: int | None = None
    rrc: int | None = None

    @property
    def any_price(self) -> bool:
        return self.purchase is not None or self.rrc is not None


@dataclass(frozen=True)
class Diff:
    """Итог сверки по одной коллекции."""
    changed: list[str]        # строки для описания задачи, по одной на позицию
    same: int                 # сколько позиций уже стоят как в прайсе
    no_price_in_file: int     # артикул есть в 1С, но в его строке прайса цены не нашлось

    @property
    def anything(self) -> bool:
        return bool(self.changed)


def to_decimal(cell) -> Decimal | None:
    """Число из ячейки прайса. «1 560,00 ₽» и «1560» — одно и то же."""
    if cell is None:
        return None
    if isinstance(cell, (int, float, Decimal)):
        value = Decimal(str(cell))
        return value if value > 0 else None
    text = _CLEAN.sub("", str(cell)).replace(",", ".").strip()
    if not text or text in {".", "-"}:
        return None
    # «1.560.00» — тысячи точкой: последняя точка отделяет копейки, прочие лишние.
    if text.count(".") > 1:
        head, _, tail = text.rpartition(".")
        text = head.replace(".", "") + "." + tail
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value > 0 else None


def resolve_columns(rows, spec: dict) -> Columns | str:
    """Перевести названное моделью в номера колонок. Ошибку возвращает текстом — он уедет
    в ответ инструмента, и модель сможет назвать колонки иначе.

    Принимается и номер (с единицы, как человек считает), и кусок заголовка: модель видит
    лист табами, без буквенных имён колонок, и заголовок для неё надёжнее счёта.
    """
    got: dict[str, int | None] = {}
    for key in ("article", *KINDS):
        raw = spec.get(key)
        if raw is None or raw == "":
            got[key] = None
            continue
        index = _index(rows, raw)
        if index is None:
            return (f"Не нашёл колонку {key}={raw!r}. Передай номер колонки с единицы "
                    f"либо точный кусок её заголовка.")
        got[key] = index

    if got["article"] is None:
        return "В price_columns обязателен article: сверка идёт по артикулу."
    cols = Columns(article=got["article"], purchase=got["purchase"], rrc=got["rrc"])
    if not cols.any_price:
        return "В price_columns нет ни одной ценовой колонки — сверять нечего."
    return cols


def _index(rows, raw) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw - 1 if raw > 0 else None
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text) - 1 if int(text) > 0 else None
    needle = text.casefold()
    # Заголовок ищем по всему листу, а не только в первой строке: у прайсов сверху
    # бывает шапка поставщика, логотип и пустые строки.
    for row in rows:
        for i, cell in enumerate(row):
            if needle in str(cell or "").casefold():
                return i
    return None


def prices_from_rows(rows, cols: Columns) -> dict[str, dict[str, Decimal]]:
    """Цены прайса по артикулам. Первая встреченная строка выигрывает: ниже по листу
    тот же артикул обычно повторяется в блоке «в упаковке» и в сводках."""
    out: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        if len(row) <= cols.article:
            continue
        key = norm_article(row[cols.article])
        if not key or key in out:
            continue
        found = {}
        for kind in KINDS:
            at = getattr(cols, kind)
            if at is not None and len(row) > at:
                value = to_decimal(row[at])
                if value is not None:
                    found[kind] = value
        if found:
            out[key] = found
    return out


def compare(items, from_price: dict[str, dict[str, Decimal]]) -> Diff:
    """Сверить позиции 1С с ценами прайса.

    `items` — записи выгрузки `by-tm` (у них `article`, `purchase`, `rrc`).
    Цены НЕТ В 1С — это расхождение, а не совпадение: писать всё равно придётся.
    """
    changed: list[str] = []
    same = 0
    no_price = 0

    for item in items:
        key = norm_article(item.article)
        wanted = from_price.get(key) if key else None
        if not wanted:
            no_price += 1
            continue

        parts = []
        for kind in KINDS:
            new = wanted.get(kind)
            if new is None:
                continue
            current = getattr(item, kind, None)
            now = Decimal(str(current.value)) if current and current.value else None
            if now is None:
                parts.append(f"{LABEL[kind]} не заполнена → {_num(new)}")
            elif not same_price(now, new):
                parts.append(f"{LABEL[kind]} {_num(now)} → {_num(new)}")
        if parts:
            changed.append(f"{item.article}: " + ", ".join(parts))
        else:
            same += 1

    return Diff(changed=changed, same=same, no_price_in_file=no_price)


def _num(value: Decimal) -> str:
    whole = value == value.to_integral_value()
    text = f"{value:,.0f}" if whole else f"{value:,.2f}"
    return text.replace(",", " ")


def report(diff: Diff) -> dict:
    """Ответ инструменту. Совпадения названы ЧИСЛОМ, а не списком: агенту нужно решение
    «заводить задачу или нет», а перечень совпавших позиций — лишние токены в истории."""
    out: dict = {"расходятся": diff.changed[:40], "совпадают": diff.same}
    if len(diff.changed) > 40:
        out["расходятся_всего"] = len(diff.changed)
    if diff.no_price_in_file:
        out["без_цены_в_прайсе"] = diff.no_price_in_file
    if not diff.anything:
        out["вывод"] = ("цены совпадают с прайсом в пределах %s%% — задачу на изменение "
                        "цен НЕ заводи" % SAME_PRICE_PCT)
    return out
