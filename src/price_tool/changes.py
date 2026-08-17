"""Что именно писать в 1С: сравнение цен, розница, порог значимости, payload set-prices.

Детерминированное ядро между сопоставлением (агент) и записью (§10 спеки). Модель сюда
передаёт только «какая коллекция/товар и какие цены в прайсе»; всё остальное считается здесь.

Порог значимости 2% — общий для всех видов цен (§9.1 спеки): изменение меньше порога не
пишется. Первая запись (цены этого вида в 1С нет) проходит всегда.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from src.onec.client import NomItem
from src.price_tool.retail import compute_retail

MIN_CHANGE_PCT = Decimal("2")


def significant(old: Decimal | None, new: Decimal | None,
                min_pct: Decimal = MIN_CHANGE_PCT) -> bool:
    """Стоит ли писать новое значение (§9.1). Нет текущего — пишем всегда."""
    if new is None or new <= 0:
        return False
    if old is None or old <= 0:
        return True
    return abs(new - old) / old * Decimal("100") >= min_pct


@dataclass
class ItemPlan:
    ref: str
    name: str
    collection_ref: str
    prices: dict[str, Decimal] = field(default_factory=dict)   # что писать
    before: dict[str, Decimal | None] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)      # вид цены → причина
    warning: str | None = None                                  # сигнал по РРЦ (§3 правил)


def plan_item(item: NomItem, new_purchase: Decimal | None, new_rrc: Decimal | None,
              today: date, min_pct: Decimal = MIN_CHANGE_PCT) -> ItemPlan:
    """Решение по одному товару: какие виды цен писать, с учётом порога и розницы."""
    cur_p = Decimal(str(item.purchase.value)) if item.purchase else None
    cur_r = Decimal(str(item.rrc.value)) if item.rrc else None
    cur_ret = Decimal(str(item.retail.value)) if item.retail else None

    plan = ItemPlan(ref=item.ref, name=item.name, collection_ref=item.collection_ref)
    plan.before = {"purchase": cur_p, "rrc": cur_r, "retail": cur_ret}

    write_p = significant(cur_p, new_purchase, min_pct)
    if new_purchase is not None:
        if write_p:
            plan.prices["purchase"] = new_purchase
        elif cur_p is not None:
            plan.skipped["purchase"] = "below_threshold" if new_purchase != cur_p else "same"

    write_r = significant(cur_r, new_rrc, min_pct)
    if new_rrc is not None:
        if write_r:
            plan.prices["rrc"] = new_rrc
        elif cur_r is not None:
            plan.skipped["rrc"] = "below_threshold" if new_rrc != cur_r else "same"

    # Розница пересчитывается только при РЕАЛЬНОМ изменении закупки (§7 правил):
    # если закупку не пишем (порог), то и повода менять розницу нет.
    eff_p = new_purchase if write_p else cur_p
    eff_r = new_rrc if write_r else cur_r
    eff_r_date = today if write_r else (
        date.fromisoformat(item.rrc.date) if item.rrc and item.rrc.date else None)

    dec = compute_retail(eff_p, rrc=eff_r, rrc_date=eff_r_date, current_retail=cur_ret,
                         today=today, purchase_changed=write_p)
    plan.warning = dec.warning
    if dec.write and dec.value is not None:
        plan.prices["retail"] = dec.value
    elif dec.value is not None:
        plan.skipped["retail"] = dec.reason
    elif dec.reason == "purchase_unchanged":
        plan.skipped["retail"] = dec.reason
    return plan


@dataclass
class GroupResult:
    tm_code: str
    tm_name: str
    collection_ref: str
    collection: str
    plans: list[ItemPlan]
    pack: Decimal | None = None      # коэффициент упаковки из alt_units — для проверки ЕИ
    unit: str = ""                   # базовая ЕИ 1С (м2, шт.) — для текста предупреждения

    @property
    def to_write(self) -> list[ItemPlan]:
        return [p for p in self.plans if p.prices]


# Скачок, за которым обычно стоит не движение рынка, а перепутанная колонка или единица
# измерения. Порог намеренно высокий: настоящие подорожания на 20–30% бывают регулярно.
JUMP_RATIO = Decimal("1.5")
# Насколько отношение новой цены к старой должно совпасть с коэффициентом упаковки,
# чтобы назвать причину прямо, а не ограничиться общим «проверьте».
PACK_TOLERANCE = Decimal("0.08")

_LABEL = {"purchase": "закупка", "rrc": "РРЦ", "retail": "розница"}
ORDER_KINDS = ("purchase", "rrc", "retail")


def _num(value: Decimal) -> str:
    return f"{value:,.0f}".replace(",", " ") if value == value.to_integral_value() \
        else f"{value:,.2f}".replace(",", " ")


SAME_PRICE_PCT = Decimal("2")     # «уже стоит столько же» — в пределах порога значимости


def _near(a: Decimal, b: Decimal, pct: Decimal = SAME_PRICE_PCT) -> bool:
    return b > 0 and abs(a - b) / b * Decimal("100") <= pct


def unit_warnings(group: GroupResult) -> list[str]:
    """Подозрительные скачки цен по коллекции — с попыткой назвать причину.

    Из прайса приходит ОДНА цена на коллекцию, а в папке 1С могут лежать товары, которые
    стоят по-разному: другая толщина, влагостойкая версия, остатки снятой позиции. Тогда
    единая цена поднимет их в разы, и это не ошибка прайса, а ошибка охвата — товар просто
    не относится к этой строке.

    Отличить это от перепутанной единицы измерения можно по самой коллекции:

    * часть позиций УЖЕ стоит новую цену → цена верна, выбиваются отдельные товары;
    * цены внутри коллекции сильно разные → одной ценой её накрывать нельзя;
    * коллекция однородна, а отношение совпало с коэффициентом упаковки → цена за
      упаковку вместо цены за базовую ЕИ (§6.3).

    Порядок важен: сначала то, что видно по данным, и только потом гипотеза про упаковку.
    Иначе совпадение отношения с коэффициентом (2.02 против 1.974 на Classen Euphoria)
    выдаётся за диагноз, хотя на деле цена была верной, а выбивалась одна позиция.
    """
    out: list[str] = []
    for kind in ORDER_KINDS:
        news = {p.prices[kind] for p in group.plans if kind in p.prices}
        if len(news) != 1:
            continue                     # розница считается по-товарно — не наш случай
        new = next(iter(news))
        currents = [(p, p.before.get(kind)) for p in group.plans]
        known = [(p, c) for p, c in currents if c is not None and c > 0]
        if not known or new <= 0:
            continue

        movers = [(p, c) for p, c in known
                  if not (JUMP_RATIO > new / c > 1 / JUMP_RATIO)]
        if not movers:
            continue

        label = _LABEL.get(kind, kind)
        at_new = [p for p, c in known if _near(c, new)]
        values = sorted(c for _, c in known)
        head = f"⚠️ {group.collection}: {label} → {_num(new)}"

        if at_new:
            names = ", ".join(f"{p.name} ({_num(c)})" for p, c in movers[:3])
            tail = f" и ещё {len(movers) - 3}" if len(movers) > 3 else ""
            out.append(
                f"{head}. {len(at_new)} из {len(known)} поз. уже стоят столько же, а эти "
                f"выбиваются: {names}{tail}. Похоже, в папке коллекции лежат РАЗНЫЕ товары "
                "— проверьте, относится ли строка прайса к ним.")
        elif values[-1] / values[0] >= JUMP_RATIO:
            out.append(
                f"{head}. В коллекции цены разные ({_num(values[0])}…{_num(values[-1])}), "
                f"а из прайса пишем одну: {len(movers)} поз. изменятся более чем в "
                f"{JUMP_RATIO} раза. Проверьте, все ли товары папки относятся к этой строке.")
        elif group.pack and group.pack > 1 and _pack_like(new / values[0], group.pack):
            out.append(
                f"{head} (было {_num(values[0])}). Отношение {new / values[0]:.2f} совпадает "
                f"с упаковкой {group.pack} — похоже, это цена ЗА УПАКОВКУ, а не за "
                f"{group.unit or 'базовую ЕИ'}. За единицу вышло бы {_num(new / group.pack)}. "
                "Проверьте колонку прайса.")
        else:
            pct = (new / values[0] - 1) * Decimal("100")
            out.append(f"{head} (было {_num(values[0])}, {pct:+.0f}%) — проверьте колонку "
                       "прайса и единицу измерения.")
    return out


def _pack_like(ratio: Decimal, pack: Decimal) -> bool:
    return abs(ratio - pack) / pack <= PACK_TOLERANCE


def plan_collection(items: list[NomItem], tm_code: str, tm_name: str,
                    new_purchase: Decimal | None, new_rrc: Decimal | None,
                    today: date, min_pct: Decimal = MIN_CHANGE_PCT) -> GroupResult:
    plans = [plan_item(i, new_purchase, new_rrc, today, min_pct) for i in items]
    first = items[0] if items else None
    # У части товаров реквизит «Коллекция» не заполнен (в каталоге таких ~130): они лежат
    # прямо в папке ТМ. Показывать админу пустое имя нельзя — берём имя папки-родителя.
    return GroupResult(tm_code=tm_code, tm_name=tm_name,
                       collection_ref=first.collection_ref if first else "",
                       collection=(first.collection or first.parent or "без коллекции")
                       if first else "",
                       plans=plans,
                       pack=_pack(first), unit=first.unit if first else "")


def _pack(item: NomItem | None) -> Decimal | None:
    """Коэффициент упаковки: сколько базовых ЕИ в одной упаковке (alt_units)."""
    if item is None or not item.alt_units:
        return None
    try:
        value = Decimal(str(next(iter(item.alt_units.values()))))
    except (StopIteration, ValueError, TypeError):
        return None
    return value if value > 0 else None


def build_payload(groups: list[GroupResult]) -> list[dict]:
    """Тело set-prices (§10.2). Форма «а» — когда по всей коллекции пишется одно и то же."""
    items: list[dict] = []
    for g in groups:
        writable = g.to_write
        if not writable:
            continue
        uniform = (len(writable) == len(g.plans)
                   and all(p.prices == writable[0].prices for p in writable))
        if uniform and g.collection_ref:
            items.append({"tm": g.tm_code, "collection_ref": g.collection_ref,
                          "prices": {k: float(v) for k, v in writable[0].prices.items()}})
        else:
            for p in writable:
                items.append({"tm": g.tm_code, "ref": p.ref,
                              "prices": {k: float(v) for k, v in p.prices.items()}})
    return items
