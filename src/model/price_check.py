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

from src.model import dealer_price
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

#: Колонки-ИСТОЧНИКИ: из них цена ВЫЧИСЛЯЕТСЯ, а не берётся как есть. Нужны прайсам без
#: закупки (решение админа 03.10.2026): дилеру дают скидку процентом от розницы, своим на
#: каждую марку, а валютную цену приводят курсом.
SOURCES = ("retail", "retail_cur")

# Мусор, который бывает в ценовой ячейке: валюта, единица, неразрывные пробелы.
_CLEAN = re.compile(r"[^\d,.\-]")


@dataclass(frozen=True)
class Columns:
    """Разрешённые номера колонок (с нуля). `article` обязателен, цены — нет.

    `retail` и `retail_cur` — РОЗНИЦА поставщика (решение админа 03.10.2026): в рублях и в
    ВАЛЮТЕ ПРАЙСА соответственно. У части прайсов закупки нет вовсе, и закупку считает код:
    розница минус скидка дилера, валютная — ещё и по курсу (`model/dealer_price.py`). Это
    ИСТОЧНИК, а не вид цены: сверяются и пишутся по-прежнему только закупка и РРЦ.

    Валюта у прайса ОДНА и указывается человеком, поэтому колонка и называется «в валюте», а
    не «в евро»: евро тут не единственный случай.
    """
    article: int
    purchase: int | None = None
    rrc: int | None = None
    retail: int | None = None
    retail_cur: int | None = None

    @property
    def any_price(self) -> bool:
        return any(value is not None for value in
                   (self.purchase, self.rrc, self.retail, self.retail_cur))

    @property
    def from_retail(self) -> bool:
        """Цены придётся считать из розницы: прямых колонок нет."""
        return (self.purchase is None and self.rrc is None
                and (self.retail is not None or self.retail_cur is not None))


@dataclass(frozen=True)
class Diff:
    """Итог сверки по одной коллекции."""
    changed: list[str]        # строки для описания задачи, по одной на позицию
    same: int                 # сколько позиций уже стоят как в прайсе
    no_price_in_file: int     # артикул есть в 1С, но в его строке прайса цены не нашлось
    no_price_in_1c: int = 0   # из них: и в 1С цены тоже нет вовсе

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
    # `retail_eur` принимается как СТАРОЕ имя `retail_cur`: валюта стала настраиваемой
    # (03.10.2026), а запомненный маппинг колонок живёт у сигнатуры и переживает выкладку.
    spec = dict(spec or {})
    if spec.get("retail_cur") in (None, "") and spec.get("retail_eur") not in (None, ""):
        spec["retail_cur"] = spec.pop("retail_eur")

    got: dict[str, int | None] = {}
    for key in ("article", *KINDS, *SOURCES):
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
    cols = Columns(article=got["article"], purchase=got["purchase"], rrc=got["rrc"],
                   retail=got["retail"], retail_cur=got["retail_cur"])
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


def prices_from_rows(rows, cols: Columns,
                     terms_for=None) -> dict[str, dict[str, Decimal]]:
    """Цены прайса по артикулам. Первая встреченная строка выигрывает: ниже по листу
    тот же артикул обычно повторяется в блоке «в упаковке» и в сводках."""
    out: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        if len(row) <= cols.article:
            continue
        cell = str(row[cols.article] or "")
        # Артикул часто слит с названием в одной ячейке: «301 Аристо». Кладём и полный
        # ключ, и первое слово — лишний ключ безвреден, недостающий срывает сверку.
        keys = [norm_article(cell)]
        head = cell.split()[0] if cell.split() else ""
        if head and norm_article(head) != keys[0]:
            keys.append(norm_article(head))
        keys = [k for k in keys if k and k not in out]
        if not keys:
            continue
        found = {}
        for kind in KINDS:
            at = getattr(cols, kind)
            if at is not None and len(row) > at:
                value = to_decimal(row[at])
                if value is not None:
                    found[kind] = value

        # ЗАКУПКИ В ПРАЙСЕ МОЖЕТ НЕ БЫТЬ ВОВСЕ (решение админа 03.10.2026). Тогда считаем
        # её из розницы: минус скидка дилера, валютную — ещё и по курсу. Прямые колонки
        # ГЛАВНЕЕ: нашлась настоящая закупка — считать нечего, в прайсе она и есть.
        if not found and cols.from_retail:
            found = _from_retail(row, cols, terms_for)

        if found:
            for key in keys:
                out[key] = found
    return out


def _from_retail(row, cols: Columns, terms_for) -> dict:
    """Закупка и РРЦ из розничной цены строки. Пустой словарь — считать нечем.

    Рублёвая розница ГЛАВНЕЕ валютной: для неё не нужен курс, то есть меньше того, что
    может быть не задано. Условия (скидка бренда, курс) спрашиваются ПО СТРОКЕ: скидка у
    каждой марки своя, а строки разных марок лежат на одном листе.
    """
    if terms_for is None:
        return {}

    for at, currency in ((cols.retail, False), (cols.retail_cur, True)):
        if at is None or len(row) <= at:
            continue
        value = to_decimal(row[at])
        if value is None:
            continue
        got = dealer_price.from_retail(value, terms_for(row) or dealer_price.Terms(),
                                       currency=currency)
        if got:
            return got
    return {}


def flat_prices(items, purchase=None, rrc=None) -> dict[str, dict[str, Decimal]]:
    """Одна цена на ВСЮ коллекцию — как её пишут в прайсе Most Floor.

    Там строка коллекции несёт цены («301 Аристо … 1880 … 2980»), а у остальных декоров
    ячейки пустые: цена общая. Колоночный разбор такую раскладку не берёт в принципе —
    он идёт по строкам, а строк с ценами всего одна. Поэтому пару чисел называет модель,
    она их и так видит, а раскладывает по позициям код.

    **АРТИКУЛ ЗДЕСЬ НИ ПРИ ЧЁМ, и это не мелочь.** Ключом был он — и позиции с пустым
    артикулом выпадали из сверки целиком. У Classen так вышло со всей коллекцией
    «Adventure WR»: девять карточек, артикул пуст у всех, номер стоит в наименовании. Пять
    из девяти держали цену 1098/1280 при прайсовой 1795/2510, а сверка молчала — сравнивать
    ей было нечего (бой 28.09.2026). Цена на коллекцию потому и «на коллекцию», что
    относится к КАЖДОЙ её позиции: ключом идёт код 1С, он есть всегда.
    """
    wanted = {}
    if purchase is not None:
        value = to_decimal(purchase)
        if value is not None:
            wanted["purchase"] = value
    if rrc is not None:
        value = to_decimal(rrc)
        if value is not None:
            wanted["rrc"] = value
    if not wanted:
        return {}
    return {i.ref: dict(wanted) for i in items if i.ref}


def compare(items, from_price: dict[str, dict[str, Decimal]]) -> Diff:
    """Сверить позиции 1С с ценами прайса.

    `items` — записи выгрузки `by-tm` (у них `article`, `purchase`, `rrc`).
    Цены НЕТ В 1С — это расхождение, а не совпадение: писать всё равно придётся.
    """
    changed: list[str] = []
    same = 0
    no_price = 0
    nameless = 0

    for item in items:
        # ДВА КЛЮЧА, и оба законные. Колоночный разбор кладёт цены по АРТИКУЛУ — он
        # единственное, что связывает строку файла с карточкой. Цена на коллекцию кладётся
        # по КОДУ 1С: она относится к каждой позиции папки, и артикула у позиции может не
        # быть вовсе (у Classen его нет у всей коллекции). Порядок важен: артикул точнее,
        # код — запасной путь.
        key = norm_article(item.article)
        ref = getattr(item, "ref", "") or ""
        wanted = (from_price.get(key) if key else None) or from_price.get(ref)
        if not wanted:
            no_price += 1
            # Цены нет НИ ТАМ, НИ ТАМ. Это не «сравнить нечем», а работа: позиция стоит
            # в 1С без цены, и знать об этом надо, даже когда прайс её строку не отдал.
            if not (item.purchase and item.purchase.value):
                nameless += 1
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
            # Называем позицию тем, что у неё есть: артикулом, а без него — кодом 1С и
            # расцветкой. «: закупка 1098 → 1795» без имени админ не прочтёт.
            who = (item.article or "").strip() or \
                f"{item.ref} {(item.site_name or item.name or '').strip()}".strip()
            changed.append(f"{who}: " + ", ".join(parts))
        else:
            same += 1

    return Diff(changed=changed, same=same, no_price_in_file=no_price,
                no_price_in_1c=nameless)


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
    if diff.no_price_in_1c:
        out["без_цены_в_1С"] = diff.no_price_in_1c

    # ВЫВОД РАЗЛИЧАЕТ ТРИ ИСХОДА, а не два. «Не с чем сравнивать» — не «совпадает»:
    # на «Классик» (A+ Floor) колонки прайса не сошлись ни с одной позицией, сравнений
    # вышло ноль, и ответ «цены совпадают, задачу НЕ заводи» был прямой ложью — при том
    # что цен в 1С не было вовсе.
    if diff.no_price_in_1c:
        out["вывод"] = ("у %d позиций в 1С цены НЕТ ВОВСЕ — задачу на изменение цен "
                        "заводи" % diff.no_price_in_1c)
    elif diff.anything:
        pass                        # расхождения перечислены, вывод очевиден
    elif diff.same:
        out["вывод"] = ("цены совпадают с прайсом в пределах %s%% — задачу на изменение "
                        "цен НЕ заводи" % SAME_PRICE_PCT)
    else:
        out["вывод"] = ("сравнить не удалось: ни одна позиция 1С не сошлась со строками "
                        "прайса — проверь price_columns, особенно колонку артикула")
    return out
