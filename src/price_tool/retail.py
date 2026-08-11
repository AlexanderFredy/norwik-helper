"""Расчёт розничной цены по specs/retail-price-rules.md.

Чистая функция без сети: вход — закупка, РРЦ с датой, текущая розница; выход — решение
писать/не писать с причиной. Частные правила по ТМ/категории/коллекции (§7) пока не заданы,
применяется базовое правило §2.

РРЦ на величину розницы НЕ влияет: обрезки нет. Свежая РРЦ ниже закупки или ниже
рассчитанной розницы — повод сообщить админу, а не изменить цену (§3).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

# §2. Пороги наценки: (верхняя граница закупки включительно, наценка %)
TIERS: tuple[tuple[Decimal | None, Decimal], ...] = (
    (Decimal("1000"), Decimal("20")),   # < 1000 → 20%  (граница обрабатывается ниже)
    (Decimal("2000"), Decimal("15")),   # 1000..2000 включительно → 15%
    (None, Decimal("12")),              # > 2000 → 12%
)

RRC_MAX_AGE_DAYS = 30      # §3: РРЦ старше — не рассматриваем даже как сигнал
MIN_CHANGE_PCT = Decimal("2")   # §6


@dataclass(frozen=True)
class RetailDecision:
    value: Decimal | None      # None — розница не формируется
    write: bool                # писать ли в 1С
    reason: str                # машинный код причины
    warning: str | None = None # сигнал по РРЦ админу (§3, §10)


def markup_pct(purchase: Decimal) -> Decimal:
    """§2. Ровно 1000 и ровно 2000 → 15%; ниже 1000 → 20%; выше 2000 → 12%."""
    if purchase < TIERS[0][0]:
        return TIERS[0][1]
    if purchase <= TIERS[1][0]:
        return TIERS[1][1]
    return TIERS[2][1]


def _round_ruble(value: Decimal) -> Decimal:
    """§5. До целого рубля, 0.5 вверх."""
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def compute_retail(
    purchase: Decimal | None,
    *,
    rrc: Decimal | None = None,
    rrc_date: date | None = None,
    current_retail: Decimal | None = None,
    today: date | None = None,
    purchase_changed: bool = True,
) -> RetailDecision:
    """Полный расчёт по §2–§6. `purchase_changed=False` → розницу не трогаем (§8)."""
    if not purchase_changed:
        return RetailDecision(None, False, "purchase_unchanged")
    if purchase is None or purchase <= 0:
        return RetailDecision(None, False, "no_purchase")

    # §2 + §5. РРЦ на величину розницы НЕ влияет — обрезки нет (§3).
    value = _round_ruble(purchase * (Decimal("1") + markup_pct(purchase) / Decimal("100")))

    # §3. Свежая РРЦ ниже закупки или ниже розницы — сигнал админу, не корректировка.
    warning = None
    fresh_rrc = (
        rrc is not None
        and rrc > 0
        and rrc_date is not None
        and today is not None
        and rrc_date >= today - timedelta(days=RRC_MAX_AGE_DAYS)
    )
    if fresh_rrc:
        if rrc <= purchase:
            warning = "rrc_below_purchase"
        elif rrc < value:
            warning = "rrc_below_retail"

    if current_retail is None or current_retail <= 0:
        return RetailDecision(value, True, "first_time", warning)

    change_pct = abs(value - current_retail) / current_retail * Decimal("100")
    if change_pct < MIN_CHANGE_PCT:              # §6
        return RetailDecision(value, False, "below_threshold", warning)

    return RetailDecision(value, True, "changed", warning)
