"""Клиент HTTP-сервиса 1С (read-only): выгружаемые ТМ и номенклатура с ценами.

Контракт — specs/content-manager.md §8. Аутентификация заголовком X-API-Token.
Ответы приходят с UTF-8 BOM, поэтому декодируем через utf-8-sig.
Синхронный клиент; при использовании из async — вызывать через asyncio.to_thread.
"""
import json
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class TradeMark:
    name: str      # NameTM, напр. "Classen / Классен"
    code: str      # Code, напр. "000000104" (строка, ведущие нули важны)


@dataclass(frozen=True)
class Price:
    value: float
    date: str | None   # день последнего изменения, ГГГГ-ММ-ДД


@dataclass(frozen=True)
class NomItem:
    ref: str                 # Код 1С (ключ записи цен)
    id: str                  # ID сайта
    name: str
    article: str             # уже .strip()
    unit: str                # базовая ЕИ
    size: str
    product_type: str
    collection: str
    parent: str
    purchase: Price | None
    rrc: Price | None


@dataclass(frozen=True)
class NomenclaturePage:
    tm: str
    total: int
    page: int
    size: int
    items: list[NomItem]


def _loads_bom(content: bytes):
    return json.loads(content.decode("utf-8-sig"))


def _price(entry: dict) -> Price | None:
    if not entry:
        return None
    try:
        return Price(value=float(entry.get("value")), date=entry.get("date") or None)
    except (TypeError, ValueError):
        return None


def _prices_to_dict(prices: list) -> dict:
    """prices — массив синглтонов [{purchase:{...}}, {rrc:{...}}] → {purchase, rrc}."""
    out: dict = {}
    for e in prices or []:
        for k, v in e.items():
            out[k] = v
    return out


class OnecClient:
    """Синхронный клиент 1С. base_url — до /api_shop/hs/ai-tools (без хвостового /)."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Token": token},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def selling_tm(self) -> list[TradeMark]:
        r = self._client.get("/get-products/selling-tm")
        r.raise_for_status()
        data = _loads_bom(r.content)
        return [TradeMark(name=x.get("NameTM", ""), code=str(x.get("Code", ""))) for x in data]

    def by_tm(self, tm_code: str, page: int = 1, size: int = 200) -> NomenclaturePage:
        r = self._client.get(
            "/get-products/by-tm",
            params={"tm": tm_code, "page": page, "size": size},
        )
        r.raise_for_status()
        data = _loads_bom(r.content)
        items = []
        for it in data.get("items", []):
            p = _prices_to_dict(it.get("prices", []))
            items.append(
                NomItem(
                    ref=str(it.get("ref", "")),
                    id=str(it.get("id", "")),
                    name=it.get("name", ""),
                    article=(it.get("article") or "").strip(),
                    unit=it.get("unit", ""),
                    size=it.get("size", ""),
                    product_type=it.get("product_type", ""),
                    collection=it.get("collection", ""),
                    parent=it.get("parent", ""),
                    purchase=_price(p.get("purchase")),
                    rrc=_price(p.get("rrc")),
                )
            )
        return NomenclaturePage(
            tm=data.get("tm", ""),
            total=int(data.get("total", 0)),
            page=int(data.get("offset", page)),
            size=int(data.get("limit", size)),
            items=items,
        )

    def by_tm_all(self, tm_code: str, size: int = 200, max_pages: int = 20) -> list[NomItem]:
        """Все страницы номенклатуры ТМ (для теста сопоставления)."""
        first = self.by_tm(tm_code, page=1, size=size)
        items = list(first.items)
        pages = (first.total + size - 1) // size if size else 1
        for page in range(2, min(pages, max_pages) + 1):
            items.extend(self.by_tm(tm_code, page=page, size=size).items)
        return items
