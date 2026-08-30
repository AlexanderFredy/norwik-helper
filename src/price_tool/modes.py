"""Режимы работы агента с прайсом (§5.2 спеки).

Прайс даёт два разных повода к правкам: расхождения в справочнике номенклатуры (§19) и
изменения цен (§9). Делать их всегда вместе не нужно.

Режим управляет НЕ ТОЛЬКО набором этапов, но и составом замечаний: в режиме «только цены»
из предложения уходит всё, что относится к справочнику, и оно становится короче и дешевле.
"""
from __future__ import annotations

SETTING = "mode"

ITEMS_PRICES = "товары+цены"
ITEMS_ONLY = "товары"
PRICES_ONLY = "цены"
DEFAULT = ITEMS_PRICES

ALL = (ITEMS_PRICES, ITEMS_ONLY, PRICES_ONLY)

# Что админ может написать в команде. Ключи нормализованы (нижний регистр, без пробелов).
_ALIASES = {
    "товары+цены": ITEMS_PRICES, "товарыицены": ITEMS_PRICES, "обе": ITEMS_PRICES,
    "оба": ITEMS_PRICES, "всё": ITEMS_PRICES, "все": ITEMS_PRICES, "both": ITEMS_PRICES,
    "товары": ITEMS_ONLY, "товар": ITEMS_ONLY, "справочник": ITEMS_ONLY,
    "items": ITEMS_ONLY,
    "цены": PRICES_ONLY, "цена": PRICES_ONLY, "prices": PRICES_ONLY,
}

_TITLES = {
    ITEMS_PRICES: "правка товаров, потом цены",
    ITEMS_ONLY: "только правка товаров",
    PRICES_ONLY: "только правка цен",
}


def parse(text: str | None) -> str | None:
    """Ответ админа → режим. None, если не распознали."""
    key = (text or "").strip().lower().replace(" ", "").replace("-", "+")
    return _ALIASES.get(key) or (key if key in ALL else None)


def title(mode: str) -> str:
    return _TITLES.get(mode, mode)


def with_items(mode: str) -> bool:
    """Правим ли справочник — и, соответственно, показываем ли замечания по нему."""
    return mode in (ITEMS_PRICES, ITEMS_ONLY)


def with_prices(mode: str) -> bool:
    return mode in (ITEMS_PRICES, PRICES_ONLY)


def describe(mode: str) -> str:
    """Блок для промпта: что делать и куда класть замечания."""
    if mode == ITEMS_ONLY:
        return ("Режим «только правка товаров»: цены НЕ трогаем. propose_prices не вызывай "
                "— он откажет. Разбирай расхождения справочника (состав коллекций, размеры, "
                "коэффициенты упаковки, несопоставленные строки) и докладывай их админу.")
    if mode == PRICES_ONLY:
        return ("Режим «только правка цен»: справочник не трогаем. Замечания, не связанные "
                "с ценами — расхождения коэффициентов ЕИ, несопоставленные строки, "
                "коллекции 1С без строк в прайсе, расхождения названий и размеров, бренды "
                "не в выгрузке — админу НЕ показывай и в item_warnings не передавай: в "
                "этом режиме он их не ждёт. Ценовые замечания оставляй как обычно.")
    return ("Режим «правка товаров, потом цены»: по каждой коллекции сначала расхождения "
            "справочника, потом предложение по ценам.")
