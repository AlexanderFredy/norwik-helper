"""Клиент HTTP-сервиса 1С (read-only): выгружаемые ТМ и номенклатура с ценами.

Контракт — specs/content-manager.md §8. Аутентификация заголовком X-API-Token.
Ответы приходят с UTF-8 BOM, поэтому декодируем через utf-8-sig.
Синхронный клиент; при использовании из async — вызывать через asyncio.to_thread.
"""
import json
import time
from dataclasses import dataclass, field

import httpx

# charset указан ЯВНО. 1С читает тело через `ПолучитьТелоКакСтроку()`, а она без charset в
# заголовке выбирает кодировку сама. Ценам это было безразлично — в их теле одни коды и
# числа, — но `set-items` повезёт кириллические наименования товаров, и неверно угаданная
# кодировка молча создаст позиции с испорченными именами. Отменить такую запись дороже, чем
# указать кодировку.
JSON_UTF8 = {"Content-Type": "application/json; charset=utf-8"}


@dataclass(frozen=True)
class TradeMark:
    name: str      # NameTM, напр. "Classen / Классен"
    code: str      # Code, напр. "000000104" (строка, ведущие нули важны)
    selling: bool = True   # помечена к выгрузке на сайт; см. selling_tm(all_marks=…)


@dataclass(frozen=True)
class Price:
    value: float
    date: str | None   # день последнего изменения, ГГГГ-ММ-ДД


@dataclass(frozen=True)
class ItemProperty:
    """Значение доп. свойства, УЖЕ проставленное у товара.

    Не путать с `PropertyOption` — там перечень допустимых значений, из которых выбирают.
    """
    property: str      # имя свойства, «Класс»
    code: str          # код свойства, «0000002»
    value: str         # имя значения, «43 класс»
    value_code: str    # код значения, «0000017»


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

    # --- поля §19.3, нужные ТОЛЬКО для правки справочника ----------------------------
    #
    # Ценовому режиму они не нужны ни одним байтом, а в товарных без них нечего сверять:
    # до расширения `by-tm` агент видел размер одной строкой `size` и не мог сказать, чем
    # карточка отличается от прайса. Значения по умолчанию оставлены пустыми намеренно —
    # старый ответ 1С (без этих полей) обязан разбираться прежним кодом.
    full_name: str = ""
    site_name: str = ""
    product_type_ref: str = ""
    collection_code: str = ""       # код ЗНАЧЕНИЯ свойства «Коллекция», не папки
    length_from: float | None = None
    length_to: float | None = None
    width_from: float | None = None
    width_to: float | None = None
    thickness: float | None = None
    properties: tuple[ItemProperty, ...] = ()
    not_exported: bool = False


@dataclass(frozen=True)
class Folder:
    """Узел дерева папок (§19.2.4).

    `kind` выводится 1С из МЕСТА узла в дереве, а не из его имени: `root` | `type` |
    `discontinued` | `tm` | `collection` | `group`. Полагаться на имя нельзя — ветки видов
    товара называются «Водостойкий ламинат» и «Двери», а не как вид товара в справочнике.
    """
    ref: str
    name: str
    parent_ref: str
    kind: str
    level: int
    not_exported: bool
    deleted: bool
    product_type_ref: str
    tm_ref: str
    tm_share: float        # доля товаров запрошенной ТМ внутри папки


@dataclass(frozen=True)
class FolderTree:
    items: list[Folder]
    total: int
    errors: list[dict] = field(default_factory=list)

    def by_ref(self, ref: str) -> Folder | None:
        return next((f for f in self.items if f.ref == ref), None)

    def children(self, ref: str) -> list[Folder]:
        return [f for f in self.items if f.parent_ref == ref]


@dataclass(frozen=True)
class PropertyOption:
    """Допустимое значение свойства — то, ИЗ ЧЕГО выбирают."""
    value: str
    code: str


@dataclass(frozen=True)
class PropertyDef:
    property: str          # имя, «Класс»
    code: str              # код свойства
    values: list[PropertyOption]


@dataclass(frozen=True)
class PropertyCatalog:
    """Каталог доп. свойств вида товара (§19.2.1).

    `matched_folder` — папка ТМ в дереве значений свойства; приходит структурой всегда,
    пустой она выглядит как `{"code": "", "name": ""}`. Пустая при запросе С МАРКОЙ значит
    «у этой марки в этой ветке коллекций нет» — не «отбор не применился».
    """
    product_type: str
    product_type_ref: str
    matched_folder: dict
    properties: list[PropertyDef]
    errors: list[dict] = field(default_factory=list)

    def by_code(self, code: str) -> PropertyDef | None:
        return next((p for p in self.properties if p.code == code), None)


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


def _number(value) -> float | None:
    """Число или None. Пустая строка и `null` от 1С — это «поле не заполнено», а не ноль:
    нулевая толщина и незаполненная толщина — разные вещи при сверке с прайсом."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _item_properties(raw) -> tuple:
    if not isinstance(raw, list):
        return ()
    return tuple(
        ItemProperty(property=(p.get("property") or "").strip(),
                     code=str(p.get("code", "")),
                     value=(p.get("value") or "").strip(),
                     value_code=str(p.get("value_code", "")))
        for p in raw if isinstance(p, dict)
    )


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

    def selling_tm(self, all_marks: bool = False) -> list[TradeMark]:
        """Торговые марки. По умолчанию — только помеченные к выгрузке на сайт.

        `all_marks=True` отдаёт и непомеченные (§19.10). Нужно, потому что марку заводят
        РАНЬШЕ, чем помечают: пока она прорабатывается, товары для неё уже создают, а
        `create_item.manufacturer` требует её код. В ПЛАН прогона прайса непомеченные не
        идут — разбирать непроработанную марку рано.
        """
        params = {"include_not_exported": 1} if all_marks else None
        r = self._get("/get-products/selling-tm", params=params)
        r.raise_for_status()
        data = _loads_bom(r.content)
        return [TradeMark(name=x.get("NameTM", ""), code=str(x.get("Code", "")),
                          selling=bool(x.get("Selling", True))) for x in data]

    def by_tm(self, tm_code: str, page: int = 1, size: int = 200,
              include_not_exported: bool = False,
              product_type: str | None = None) -> NomenclaturePage:
        """Номенклатура марки постранично.

        `include_not_exported` включает НЕВЫГРУЖАЕМЫЕ позиции — прежде всего снятые с
        производства (§19.3). Без него проверка «нет ли товара среди снятых» невозможна:
        папка снятых помечена «Не выгружать», и агент завёл бы дубль вместо возврата.
        В ценовом режиме флаг не нужен — цены снятым не пишут.
        """
        params: dict = {"tm": tm_code, "page": page, "size": size}
        if include_not_exported:
            params["include_not_exported"] = 1
        if product_type:
            params["product_type"] = product_type
        r = self._get("/get-products/by-tm", params=params)
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
                    full_name=it.get("full_name", ""),
                    site_name=it.get("site_name", ""),
                    product_type_ref=str(it.get("product_type_ref", "")),
                    collection_code=str(it.get("collection_code", "")),
                    length_from=_number(it.get("length_from")),
                    length_to=_number(it.get("length_to")),
                    width_from=_number(it.get("width_from")),
                    width_to=_number(it.get("width_to")),
                    thickness=_number(it.get("thickness")),
                    properties=_item_properties(it.get("properties")),
                    not_exported=bool(it.get("not_exported", False)),
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

    def folders(self, product_type: str | None = None,
                tm: str | None = None) -> FolderTree:
        """Дерево папок номенклатуры (§19.2.4).

        Оба фильтра необязательны, но звать без них дорого: на боевой базе дерево — 1 290
        узлов. С `tm` возвращаются папки марки И все папки, где лежат её товары, — включая
        чужие ветки, куда позиции попали по ошибке (§19.6.1).
        """
        params: dict = {}
        if product_type:
            params["product_type"] = product_type
        if tm:
            params["tm"] = tm
        r = self._get("/get-products/folders", params=params or None)
        r.raise_for_status()
        data = _loads_bom(r.content)
        items = [
            Folder(
                ref=str(x.get("ref", "")),
                name=(x.get("name") or "").strip(),
                parent_ref=str(x.get("parent_ref", "")),
                kind=x.get("kind", ""),
                level=int(x.get("level", 0) or 0),
                not_exported=bool(x.get("not_exported", False)),
                deleted=bool(x.get("deleted", False)),
                product_type_ref=str(x.get("product_type_ref", "")),
                tm_ref=str(x.get("tm_ref", "")),
                tm_share=float(x.get("tm_share", 0) or 0),
            )
            for x in (data.get("items") or [])
        ]
        return FolderTree(items=items, total=int(data.get("total", len(items)) or 0),
                          errors=[e for e in (data.get("errors") or [])
                                  if isinstance(e, dict)])

    def properties_by_type(self, type_code: str, tm: str | None = None,
                           property_code: str | None = None) -> PropertyCatalog:
        """Каталог доп. свойств вида товара со значениями (§19.2.1).

        `property_code` отбирает ОДНО свойство. Для «Коллекции» это обязательно вместе с
        `tm`: без отбора она одна весит 3 130 значений — больше всего остального каталога
        вместе взятого (§9.6.3).
        """
        params: dict = {"type-code": type_code}
        if tm:
            params["tm"] = tm
        if property_code:
            params["property"] = property_code
        r = self._get("/get-products/properties-by-type", params=params)
        r.raise_for_status()
        data = _loads_bom(r.content)
        props = [
            PropertyDef(
                property=(p.get("property") or "").strip(),
                code=str(p.get("code", "")),
                values=[PropertyOption(value=(v.get("value") or "").strip(),
                                       code=str(v.get("code", "")))
                        for v in (p.get("values") or [])],
            )
            for p in (data.get("properties") or [])
        ]
        matched = data.get("matched_folder")
        return PropertyCatalog(
            product_type=(data.get("product_type") or "").strip(),
            product_type_ref=str(data.get("product_type_ref", "")),
            matched_folder=matched if isinstance(matched, dict) else {"code": "", "name": ""},
            properties=props,
            errors=[e for e in (data.get("errors") or []) if isinstance(e, dict)],
        )

    def set_items(self, ops: list[dict]) -> dict:
        """ЕДИНСТВЕННАЯ запись в справочник номенклатуры (§19.8).

        Как и `set_prices`, вызывается ТОЛЬКО после кнопки админа — модели метод недоступен.
        Батч не откатывается целиком: разбирать результат обязан вызывающий, потому что
        часть операций может быть пропущена по зависимости (`skipped_dependency`).
        """
        body = json.dumps({"items": ops}, ensure_ascii=False).encode("utf-8")
        r = self._client.post("/get-products/set-items", content=body,
                              headers=JSON_UTF8, timeout=300)
        text = r.content.decode("utf-8-sig", errors="replace")
        if "<!DOCTYPE" in text:
            raise RuntimeError(f"1С вернул HTML вместо JSON (HTTP {r.status_code})")
        r.raise_for_status()
        return json.loads(text)

    def set_prices(self, items: list[dict]) -> dict:
        """ЕДИНСТВЕННАЯ операция записи (§10). Возвращает разобранный ответ 1С.

        Вызывается только после явного подтверждения админа — гейт реализован в боте
        (кнопка), модели этот метод недоступен.
        """
        body = json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")
        r = self._client.post("/get-products/set-prices", content=body,
                              headers=JSON_UTF8, timeout=300)
        text = r.content.decode("utf-8-sig", errors="replace")
        if "<!DOCTYPE" in text:
            raise RuntimeError(f"1С вернул HTML вместо JSON (HTTP {r.status_code})")
        r.raise_for_status()
        return json.loads(text)

    def by_tm_all(self, tm_code: str, size: int = 200, max_pages: int = 20,
                  include_not_exported: bool = False,
                  product_type: str | None = None) -> Nomenclature:
        """Все страницы номенклатуры ТМ вместе с ошибками отдельных позиций."""
        kw = {"include_not_exported": include_not_exported, "product_type": product_type}
        first = self.by_tm(tm_code, page=1, size=size, **kw)
        items = list(first.items)
        errors = list(first.errors)
        pages = (first.total + size - 1) // size if size else 1
        for page in range(2, min(pages, max_pages) + 1):
            chunk = self.by_tm(tm_code, page=page, size=size, **kw)
            items.extend(chunk.items)
            errors.extend(chunk.errors)
        return Nomenclature(tm=first.tm or tm_code, total=first.total, items=items,
                            errors=errors)
