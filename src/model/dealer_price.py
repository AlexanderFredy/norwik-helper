"""Закупка из розницы: скидка дилеру и курс валюты (решение админа 03.10.2026).

**ЗАЧЕМ.** У части поставщиков закупочных цен в прайсе нет вовсе. «Остатки 01.10.2026» —
«Розничная цена в рублях», «Розничная цена в евро», свободный остаток и наличие; ни
«закупки», ни «опта», ни «дилерской». Дилеру такой поставщик даёт **скидку процентом от
розницы**, причём СВОИМ процентом на каждую марку, а валютную цену надо приводить курсом.

**СЧИТАЕТ КОД, А НЕ МОДЕЛЬ.** Формула подтверждена админом 03.10.2026:

    закупка = розница × (1 − скидка/100)
    РРЦ     = розница

Умножить два числа — не работа для рассуждений, а ошибка здесь необратима: цена уезжает в
1С. От модели нужно ровно одно, чего код не знает: КАКАЯ КОЛОНКА листа розничная (и какая из
них валютная) — это она читает глазами и называет один раз на лист.

**НЕТ СКИДКИ ИЛИ КУРСА — ЦЕН НЕ СЧИТАЕМ ВОВСЕ.** Ни сверки, ни записи, ни задачи по ценам:
вместо них говорим, чего не хватает. Посчитать по пустому курсу значит записать в 1С цифру,
которой нет, и обратно её не достать.

**НОЛЬ ЧИТАЕТСЯ КАК «НЕ ЗАДАНО», и это не придирка.** В 1С `Скидка` и `КурсЕвро` — числовые
реквизиты, у них пустого значения не бывает: незаполненное поле приезжает нулём. Считать
ноль настоящей скидкой значило бы молча записать закупку, равную рознице, — то есть потерять
всю маржу на целой марке. Скидки 0% дилеру не дают; нужен такой случай — он задаётся не
здесь, а отдельным решением.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

#: Копейки. Цена уезжает в 1С числом, и хвост в десять знаков там никому не нужен.
CENTS = Decimal("0.01")

#: Что в шапке означает закупку. Нашлось — пересчитывать нечего, прайс обычный.
_PURCHASE = re.compile(r"закуп|опт|дилер|dealer|wholesale", re.I)
#: Что означает розницу. Корень «розн», а не «розниц»: в боевом файле колонка называется
#: «Розничная цена в рублях» — там «розничн», и шаблон «розниц» её не находил (поймано на
#: живом файле 03.10.2026, до того признак молча считал прайс обычным).
_RETAIL = re.compile(r"розн|retail|ррц|мрц|rrp", re.I)
#: Что означает валюту. Евро — единственная валюта, встреченная живьём.
_EURO = re.compile(r"евро|eur|€", re.I)


@dataclass(frozen=True)
class Terms:
    """Чем розница превращается в закупку. Ноль и None — одно и то же: «не задано»."""
    discount: float | None = None
    rate: float | None = None

    @property
    def has_discount(self) -> bool:
        return bool(self.discount) and 0 < float(self.discount) < 100

    @property
    def has_rate(self) -> bool:
        return bool(self.rate) and float(self.rate) > 0


@dataclass(frozen=True)
class Shape:
    """Что за цены в шапке листа. Нужна, чтобы СКАЗАТЬ модели, как их называть."""
    purchase: bool = False
    retail: bool = False
    euro: bool = False

    @property
    def retail_only(self) -> bool:
        """Закупки нет, розница есть — тот самый случай, ради которого всё это."""
        return self.retail and not self.purchase


def shape(rows, depth: int = 20) -> Shape:
    """Какие цены обещает шапка листа.

    Смотрим только шапку: ниже в данных попадается слово «розница» в названии товара, и по
    всему листу признак срабатывал бы где угодно.
    """
    text = " ".join(str(cell or "") for row in rows[:depth] for cell in row)
    return Shape(purchase=bool(_PURCHASE.search(text)),
                 retail=bool(_RETAIL.search(text)),
                 euro=bool(_EURO.search(text)))


def missing(terms: Terms, currency: bool = False) -> str:
    """Чего не хватает, чтобы считать. Пустая строка — хватает всего.

    Текст уезжает и в ответ инструмента, и в отчёт админу: «цены не записаны» без причины
    читается как сбой, а причина тут всегда одна из двух и обе чинятся в форме брендов.
    """
    gaps = []
    if not terms.has_discount:
        gaps.append("скидка дилера от розницы (% у бренда)")
    if currency and not terms.has_rate:
        gaps.append("курс валюты у прайса")
    if not gaps:
        return ""
    return ("не задано: " + "; ".join(gaps)
            + ". Задаётся в форме «Бренды» в 1С; без этого закупку считать нельзя.")


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def from_retail(retail, terms: Terms, currency: bool = False) -> dict:
    """Розница → `{"purchase": …, "rrc": …}`. Пустой словарь — считать нечем.

    РРЦ — это и есть розница поставщика, приведённая к рублям: другого источника
    рекомендованной цены в таком прайсе нет, а выдумывать её нельзя.
    """
    if retail is None:
        return {}
    value = Decimal(str(retail))
    if value <= 0:
        return {}

    if currency:
        if not terms.has_rate:
            return {}
        value = value * Decimal(str(terms.rate))

    if not terms.has_discount:
        return {}

    purchase = value * (Decimal("1") - Decimal(str(terms.discount)) / Decimal("100"))
    return {"purchase": _money(purchase), "rrc": _money(value)}


def explain(terms: Terms, currency: bool = False) -> str:
    """Одна строка о том, по чему считаем, — для описания задачи и ответа инструмента.

    Нужна, чтобы цифры в задаче можно было проверить глазами: закупка в прайсе не стоит, и
    без этой строки её происхождение неизвестно никому, включая админа через месяц.
    """
    if not terms.has_discount:
        return ""
    parts = [f"закупка = розница − {_num(terms.discount)}%"]
    if currency and terms.has_rate:
        parts.append(f"розница в валюте по курсу {_num(terms.rate)}")
    return "; ".join(parts)


def _num(value) -> str:
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") if text else "0"
