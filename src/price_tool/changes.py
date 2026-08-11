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

    @property
    def to_write(self) -> list[ItemPlan]:
        return [p for p in self.plans if p.prices]


def plan_collection(items: list[NomItem], tm_code: str, tm_name: str,
                    new_purchase: Decimal | None, new_rrc: Decimal | None,
                    today: date, min_pct: Decimal = MIN_CHANGE_PCT) -> GroupResult:
    plans = [plan_item(i, new_purchase, new_rrc, today, min_pct) for i in items]
    first = items[0] if items else None
    return GroupResult(tm_code=tm_code, tm_name=tm_name,
                       collection_ref=first.collection_ref if first else "",
                       collection=first.collection if first else "",
                       plans=plans)


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
