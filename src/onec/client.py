"""Клиент HTTP-сервиса 1С (read-only): выгружаемые ТМ и номенклатура с ценами.

Контракт — specs/content-manager.md §8. Аутентификация заголовком X-API-Token.
Ответы приходят с UTF-8 BOM, поэтому декодируем через utf-8-sig.
Синхронный клиент; при использовании из async — вызывать через asyncio.to_thread.
"""
import json
import time
from dataclasses import dataclass, field

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
    parent: str                # имя папки-родителя (≈ коллекция)
    collection_ref: str        # Код папки-родителя — идентификатор для set-prices, форма (а)
    alt_units: dict          # {ЕИ: коэффициент к базовой}, напр. {"упак": 2.367}
    purchase: Price | None
    retail: Price | None       # розничная цена YO-000004 (specs/retail-price-rules.md)
    rrc: Price | None


@dataclass(frozen=True)
class NomenclaturePage:
    tm: str
    total: int
    page: int
    size: int
    items: list[NomItem]
    errors: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Nomenclature:
    """Вся номенклатура ТМ: страницы склеены, ошибки позиций собраны.

    `errors` — позиции, которые 1С отдать не смогла (§ by-tm.bsl: сбой на одном товаре
    больше не роняет запрос). Их нельзя молча терять: сопоставление с прайсом окажется
    неполным, и админ должен об этом узнать.
    """
    tm: str
    total: int
    items: list[NomItem]
    errors: list[dict] = field(default_factory=list)


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


def _parent_name(parent) -> str:
    """parent — объект {code, name}; ранняя версия сервиса отдавала просто строку."""
    if isinstance(parent, dict):
        return parent.get("name") or ""
    return parent or ""


def _parent_code(parent) -> str:
    return parent.get("code") or "" if isinstance(parent, dict) else ""


def _alt_units_to_dict(alt_units: list) -> dict:
    """alt_units — массив синглтонов [{"упак": 2.367}] → {"упак": 2.367} (float)."""
    out: dict = {}
    for e in alt_units or []:
        for k, v in e.items():
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                continue
    return out


class OnecClient:
    """Синхронный клиент 1С. base_url — до /api_shop/hs/ai-tools (без хвостового /)."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0,
                 retries: int = 5) -> None:
        self._retries = max(1, retries)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Token": token},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        """GET с повторами: сервис 1С периодически не принимает соединение (WinError 10060)."""
        last: Exception | None = None
        for attempt in range(self._retries):
            try:
                return self._client.get(path, params=params)
            except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout) as exc:
                last = exc
                time.sleep(2 * (attempt + 1))
        raise last  # type: ignore[misc]

    def selling_tm(self) -> list[TradeMark]:
        r = self._get("/get-products/selling-tm")
        r.raise_for_status()
        data = _loads_bom(r.content)
        return [TradeMark(name=x.get("NameTM", ""), code=str(x.get("Code", ""))) for x in data]

    def by_tm(self, tm_code: str, page: int = 1, size: int = 200) -> NomenclaturePage:
        r = self._get(
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
                    parent=_parent_name(it.get("parent")),
                    collection_ref=_parent_code(it.get("parent")),
                    alt_units=_alt_units_to_dict(it.get("alt_units", [])),
                    purchase=_price(p.get("purchase")),
                    retail=_price(p.get("retail")),
                    rrc=_price(p.get("rrc")),
                )
            )
        return NomenclaturePage(
            tm=data.get("tm", ""),
            total=int(data.get("total", 0)),
            page=int(data.get("offset", page)),
            size=int(data.get("limit", size)),
            items=items,
            errors=[e for e in (data.get("errors") or []) if isinstance(e, dict)],
        )

    def set_prices(self, items: list[dict]) -> dict:
        """ЕДИНСТВЕННАЯ операция записи (§10). Возвращает разобранный ответ 1С.

        Вызывается только после явного подтверждения админа — гейт реализован в боте
        (кнопка), модели этот метод недоступен.
        """
        body = json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")
        r = self._client.post("/get-products/set-prices", content=body,
                              headers={"Content-Type": "application/json"}, timeout=300)
        text = r.content.decode("utf-8-sig", errors="replace")
        if "<!DOCTYPE" in text:
            raise RuntimeError(f"1С вернул HTML вместо JSON (HTTP {r.status_code})")
        r.raise_for_status()
        return json.loads(text)

    def by_tm_all(self, tm_code: str, size: int = 200, max_pages: int = 20) -> Nomenclature:
        """Все страницы номенклатуры ТМ вместе с ошибками отдельных позиций."""
        first = self.by_tm(tm_code, page=1, size=size)
        items = list(first.items)
        errors = list(first.errors)
        pages = (first.total + size - 1) // size if size else 1
        for page in range(2, min(pages, max_pages) + 1):
            chunk = self.by_tm(tm_code, page=page, size=size)
            items.extend(chunk.items)
            errors.extend(chunk.errors)
        return Nomenclature(tm=first.tm or tm_code, total=first.total, items=items,
                            errors=errors)
