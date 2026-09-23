"""Выбор НАИМЕНЬШЕЙ АКТУАЛЬНОЙ цены среди поставщиков (§6.4 модели).

**Задача.** Один и тот же товар возят несколько поставщиков, и цены у них разные. В 1С
должна стоять наименьшая из ДЕЙСТВУЮЩИХ — а агент, разбирая прайс подороже, не должен
затирать ею цену подешевле.

**Актуальность нельзя проверить, её можно только датировать.** Единственное доказательство
живой цены — новый прайс; спросить поставщика система не может. Поэтому решение принимается
по датам, и ошибка сдвинута в безопасную сторону: лучше записать честно подорожавшую цену,
чем держать в базе цифру, по которой сегодня никто не продаёт. Первое стоит недополученной
маржи, второе — продажи в минус.

**Свежесть считается ОТНОСИТЕЛЬНО обрабатываемого прайса, а не «от сегодня».** Это работает
потому, что момент записи всегда содержит хотя бы одно свежее предложение — то, ради
которого мы и пишем. Сравнивать два протухших предложения не приходится никогда.

**Окно — 180 дней** (решение админа 22.09.2026). Предложение старше этого не участвует в
сравнении вовсе, но и не пропадает молча: о нём говорится админу, потому что дешёвая
протухшая цена — это повод запросить свежий прайс, а не мусор.

**Эксклюзивы правило НЕ обходит** — тоже решение админа: пометка эксклюзива справочная и
на цены никогда не влияла, незачем начинать.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

#: Насколько предложение может быть старше обрабатываемого прайса, чтобы ещё считаться
#: сопоставимым. Прайсы приходят примерно раз в месяц; полгода — это ещё «тот же сезон»,
#: а год давности про цену не говорит уже ничего.
FRESH_WINDOW_DAYS = 180


@dataclass(frozen=True)
class Offer:
    """Что просит один поставщик за одну позицию. Цены — в базовой ЕИ товара."""
    supplier_id: int
    supplier: str
    purchase: float | None
    rrc: float | None = None
    price_date: str | None = None

    @property
    def when(self) -> date | None:
        return _day(self.price_date)

    def label(self) -> str:
        who = self.supplier or f"поставщик №{self.supplier_id}"
        out = f"«{who}» {_num(self.purchase)}"
        if self.price_date:
            out += f" (прайс от {self.price_date[:10]})"
        return out


@dataclass(frozen=True)
class Choice:
    """Кого выбрали и что сказать админу."""
    offer: Offer
    notes: list[str]

    @property
    def taken_from_others(self) -> bool:
        return bool(self.notes) and self.offer.supplier_id != 0


def best(asking: Offer, others: list[Offer]) -> Choice:
    """Чью цену писать. `asking` — предложение обрабатываемого прайса.

    Побеждает наименьшая закупка среди свежих. При равенстве цен побеждает обрабатываемый
    прайс: менять поставщика ради нуля незачем, а история цен станет чище.
    """
    if asking.purchase is None:
        return Choice(asking, [])

    fresh, stale = [], []
    for offer in others:
        if offer.purchase is None or offer.supplier_id == asking.supplier_id:
            continue
        (fresh if _comparable(offer, asking) else stale).append(offer)

    notes: list[str] = []
    winner = asking
    for offer in fresh:
        if offer.purchase < winner.purchase:
            winner = offer

    if winner is not asking:
        notes.append(f"цена взята у другого поставщика: {winner.label()} "
                     f"против {_num(asking.purchase)}")

    # ПРОТУХШАЯ ВЫГОДА — НЕ МУСОР. Она не влияет на запись, но админу о ней сказать надо:
    # это повод запросить у поставщика свежий прайс, а не потерянная строка.
    for offer in stale:
        if offer.purchase < winner.purchase:
            notes.append(
                f"дешевле было у {offer.label()}, но прайс старше на "
                f"{_age(offer, asking)} дн. — в сравнение не брался, есть смысл "
                f"запросить свежий")
    return Choice(winner, notes)


def _comparable(offer: Offer, asking: Offer) -> bool:
    """Предложение сопоставимо, если оно не старше обрабатываемого прайса на окно.

    Более СВЕЖЕЕ предложение сопоставимо всегда: свежести много не бывает. Даты нет ни у
    одного из двух — считаем сопоставимым: отбрасывать из-за незаполненного поля хуже,
    чем сравнить.
    """
    days = _age(offer, asking)
    return days is None or days <= FRESH_WINDOW_DAYS


def _age(offer: Offer, asking: Offer) -> int | None:
    """На сколько дней предложение СТАРШЕ обрабатываемого прайса. Отрицательных нет."""
    mine, theirs = asking.when, offer.when
    if mine is None or theirs is None:
        return None
    return max((mine - theirs).days, 0)


def _day(value) -> date | None:
    if not value:
        return None
    text = str(value)[:10]
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _num(value) -> str:
    if value is None:
        return "—"
    number = float(value)
    whole = abs(number - round(number)) < 0.005
    text = f"{round(number):,}" if whole else f"{number:,.2f}"
    return text.replace(",", " ")


def stale_edge(asking_date: str | None) -> date | None:
    """Граница сопоставимости — для отчётов и тестов."""
    day = _day(asking_date)
    return None if day is None else day - timedelta(days=FRESH_WINDOW_DAYS)
