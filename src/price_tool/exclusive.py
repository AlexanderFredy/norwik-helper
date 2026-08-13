"""Эксклюзивы поставщиков: кто из них возит ТМ/коллекцию единолично (§9.5 спеки).

Пометка **чисто информативная**: она не влияет ни на цены, ни на выбор поставщика, ни на
запись в 1С. Единственное действие, которое она порождает, — вопрос админу, когда двое
заявили эксклюзив на одно и то же.

Два слоя данных, как и в сравнении поставщиков (§9.4):

* **заявки** (`exclusive_claims`) — что поставщик написал в прайсе; копятся при каждом
  разборе, в том числе отклонённого прайса, и никогда не перезаписываются;
* **решения** (`exclusive_decisions`) — ответ админа; перекрывает заявки, в том числе
  снимает пометку совсем (`supplier is None`).

Действующая пометка не хранится, а **выводится** из этих двух — иначе её пришлось бы
пересчитывать при каждой новой заявке и держать в согласованном состоянии.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Заявки разных поставщиков, попавшие в это окно по ДАТЕ ПРАЙСА, считаются одновременными
# и спорят между собой. Разнесённые дальше — это не спор, а смена эксклюзива: побеждает
# свежая. «Диапазон 2 месяца» из постановки, с запасом на разную длину месяцев.
DISPUTE_WINDOW_DAYS = 62

# Заявка без подтверждения новым прайсом живёт год. Прайсы приходят примерно ежемесячно,
# поэтому живой эксклюзив подтверждается многократно; молчание длиной в год означает, что
# договорённость истекла, а мы бы иначе показывали неправду годами.
CLAIM_TTL_DAYS = 365

# Подсказка модели, а НЕ детектор: решение о том, на что распространяется надпись,
# принимается по расположению в таблице, и одной регуляркой оно не берётся.
HINT_WORDS = (
    "эксклюзив", "эксклюзивно", "эксклюзивный", "эксклюзивная", "экскл.",
    "только у нас", "exclusive", "exclusively", "exclusive distributor",
)

WHERE_FOUND = ("column", "header", "sheet", "filename", "admin")


@dataclass(frozen=True)
class Claim:
    """Одна заявка об эксклюзиве из одного прайса."""
    supplier: str
    tm_code: str
    tm_name: str = ""
    collection_ref: str = ""
    collection: str = ""
    item_ref: str = ""
    item_name: str = ""
    phrase: str = ""
    where_found: str = ""
    price_date: str = ""
    price_doc: str = ""


@dataclass(frozen=True)
class Decision:
    """Ответ админа. `supplier is None` — «эксклюзива нет», пометка снимается."""
    tm_code: str
    collection_ref: str = ""
    item_ref: str = ""
    supplier: str | None = None
    decided_at: str = ""
    note: str = ""


@dataclass(frozen=True)
class Exclusive:
    """Действующая пометка на объекте."""
    tm_code: str
    collection_ref: str
    item_ref: str
    supplier: str
    since: str                      # дата прайса-основания либо решения админа
    tm_name: str = ""
    collection: str = ""
    item_name: str = ""
    phrase: str = ""
    by_admin: bool = False


@dataclass(frozen=True)
class Dispute:
    """Двое и больше заявили эксклюзив на один объект в пределах окна."""
    tm_code: str
    collection_ref: str
    item_ref: str
    suppliers: tuple[str, ...]
    claims: tuple[Claim, ...]
    tm_name: str = ""
    collection: str = ""
    item_name: str = ""

    @property
    def title(self) -> str:
        return _title(self.tm_name or self.tm_code, self.collection, self.item_name)


Key = tuple[str, str, str]


def _key(obj) -> Key:
    return (obj.tm_code or "", obj.collection_ref or "", obj.item_ref or "")


def _title(tm: str, collection: str, item: str) -> str:
    return item or " / ".join(p for p in (tm, collection) if p) or tm


def _day(iso: str | None) -> date | None:
    try:
        return date.fromisoformat((iso or "")[:10])
    except ValueError:
        return None


def resolve(claims, decisions=(), today: str | None = None
            ) -> tuple[dict[Key, Exclusive], list[Dispute]]:
    """Заявки + решения → (действующие пометки, споры к разрешению админом).

    Спорный объект НЕ получает пометки: показать «эксклюзив Монарха», когда с равным
    основанием это может быть эксклюзив другого поставщика, хуже, чем не показать ничего.
    """
    now = _day(today) or date.today()
    by_key: dict[Key, list[Claim]] = {}
    for claim in claims:
        by_key.setdefault(_key(claim), []).append(claim)

    decided = {_key(d): d for d in decisions}
    active: dict[Key, Exclusive] = {}
    disputes: list[Dispute] = []

    for key in dict.fromkeys([*decided, *by_key]):
        group = sorted(by_key.get(key, []), key=lambda c: c.price_date or "")
        last = group[-1] if group else None
        decision = decided.get(key)

        if decision is not None:
            if decision.supplier:
                active[key] = Exclusive(
                    tm_code=key[0], collection_ref=key[1], item_ref=key[2],
                    supplier=decision.supplier, since=decision.decided_at,
                    tm_name=last.tm_name if last else "",
                    collection=last.collection if last else "",
                    item_name=last.item_name if last else "",
                    phrase=decision.note, by_admin=True)
            continue                        # supplier is None — пометка снята админом

        fresh = [c for c in group
                 if (d := _day(c.price_date)) and (now - d).days <= CLAIM_TTL_DAYS]
        if not fresh:
            continue

        newest = _day(fresh[-1].price_date)
        window = [c for c in fresh
                  if (newest - _day(c.price_date)).days <= DISPUTE_WINDOW_DAYS]
        suppliers = list(dict.fromkeys(c.supplier for c in window))

        if len(suppliers) > 1:
            disputes.append(Dispute(
                tm_code=key[0], collection_ref=key[1], item_ref=key[2],
                suppliers=tuple(suppliers), claims=tuple(window),
                tm_name=fresh[-1].tm_name, collection=fresh[-1].collection,
                item_name=fresh[-1].item_name))
            continue

        winner = fresh[-1]
        active[key] = Exclusive(
            tm_code=key[0], collection_ref=key[1], item_ref=key[2],
            supplier=winner.supplier, since=winner.price_date,
            tm_name=winner.tm_name, collection=winner.collection,
            item_name=winner.item_name, phrase=winner.phrase)

    return active, disputes


def find(active: dict[Key, Exclusive], tm_code: str | None,
         collection_ref: str | None = None, item_ref: str | None = None) -> Exclusive | None:
    """Пометка на объекте с наследованием: товар → его коллекция → вся ТМ."""
    if not tm_code:
        return None
    tm, coll, item = tm_code, collection_ref or "", item_ref or ""
    for key in ((tm, coll, item), (tm, coll, ""), (tm, "", "")):
        found = active.get(key)
        if found:
            return found
    return None


def label(exc: Exclusive | None) -> str:
    """«эксклюзив: Монарх Логистик».

    Именительный падеж намеренно: родительный от произвольного названия компании кодом не
    построить — «ТД Паркет» превратился бы в «ТД Паркета». Модель в свободном ответе
    просклоняет сама, генератору отчётов это не нужно.
    """
    return f"эксклюзив: {exc.supplier}" if exc else ""


def annotate(text: str, exc: Exclusive | None) -> str:
    """«Adventure» → «Adventure (эксклюзив: Монарх Логистик)»."""
    return f"{text} ({label(exc)})" if exc else text


def prompt_block(active: dict[Key, Exclusive]) -> str:
    """Компактный список для системного промпта менеджерского агента.

    Вкладывается в промпт, а не отдаётся инструментом: инструмент модель может не позвать,
    и пометка молча исчезнет — для информационной подписи это худший режим отказа.
    """
    if not active:
        return ""
    lines = []
    for exc in sorted(active.values(), key=lambda e: (e.tm_name or e.tm_code, e.collection)):
        lines.append(f"- {_title(exc.tm_name or exc.tm_code, exc.collection, exc.item_name)}"
                     f" — {exc.supplier}")
    return (
        "\n## Эксклюзивы поставщиков\n\n"
        "Эти позиции и коллекции возит один поставщик. Если отвечаешь про что-то из "
        "списка, добавь это после названия — например «Classen Adventure (эксклюзив "
        "Монарха)». Пометка справочная: на цены, наличие и выбор поставщика она не "
        "влияет, сам ничего из неё не выводи и в список ничего не дописывай.\n\n"
        + "\n".join(lines) + "\n")
