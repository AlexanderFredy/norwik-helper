"""Клиент HTTP-сервиса 1С (read-only): выгружаемые ТМ и номенклатура с ценами.

Контракт — specs/content-manager.md §8. Аутентификация заголовком X-API-Token.
Ответы приходят с UTF-8 BOM, поэтому декодируем через utf-8-sig.
Синхронный клиент; при использовании из async — вызывать через asyncio.to_thread.
"""
import json
import logging
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
class FoundItem:
    """Позиция, найденная поиском по всей номенклатуре (§19.11).

    Отдельно от `NomItem`: там цены и коэффициенты ЕИ, здесь их нет и быть не должно —
    поиск отвечает на вопрос «существует ли уже такой товар и где он лежит», а не «почём».
    """
    ref: str
    name: str
    full_name: str = ""
    article: str = ""
    unit: str = ""
    product_type: str = ""
    product_type_ref: str = ""
    tm: str = ""
    tm_code: str = ""
    parent_ref: str = ""
    parent_name: str = ""
    not_exported: bool = False


@dataclass(frozen=True)
class FoundItems:
    items: list[FoundItem]
    total: int
    truncated: bool = False
    errors: list[dict] = field(default_factory=list)


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


logger = logging.getLogger(__name__)

#: Позиций в одной странице выгрузки номенклатуры.
#:
#: Замер на боевой базе 24.09.2026: страница из 1 позиции — 1,2 с, из 5 — 1,1 с, из 10 —
#: 1,2 с. Время НЕ ЗАВИСИТ от размера: платим за запрос по марке, позиции внутри стоят
#: сотые доли. А вот надёжность зависит: на 25 позициях ответ не пришёл дважды подряд,
#: пока соседние одиночные запросы проходили за секунду. Где именно проходит граница,
#: чисто замерить не удалось — мешала собственная нагрузка агента, — поэтому берём размер
#: ЗАВЕДОМО НИЖЕ последнего проверенно живого (10).
#:
#: ВОСЬМЁРКА — ЭТО ОБХОД VPN, А НЕ ПРЕДЕЛ 1С. Разбор 25.09.2026 закончился журналом IIS,
#: и он снял все прежние версии разом:
#:
#:     2026-09-25 05:08:28  by-tm  tm=000000005&include_not_exported=1&page=1&size=8
#:                          c-ip 79.127.196.75  →  200  0  0  time-taken 1176
#:
#: Это ровно тот запрос, который снаружи не отдавал НИ ОДНОГО БАЙТА (ни заголовков) и
#: обрывался по нашему таймауту двенадцать раз подряд. IIS ответил успешно за 1,2 с, а
#: ответ пропал в туннеле VPN, через который работает станция агента. Без VPN тот же
#: запрос проходит так же быстро, как на самом сервере.
#:
#: За сутки в журнале 13 719 ответов со статусом 200 и нулевым `sc-win32-status`, а самый
#: медленный `by-tm` — 6,8 с, и это страница на ДВЕСТИ позиций со снятыми. Значит не
#: существовало ни «больных карточек», ни «предела по объёму ответа», ни «дефекта
#: обработчика»: всё это были потери в туннеле, и размер страницы уменьшался напрасно.
#:
#: Поднимать обратно — когда путь до 1С перестанет идти через VPN (или для его адреса
#: появится маршрут в обход туннеля). Порядок такой: `tests.integration_onec_health`
#: печатает лестницу размеров, и `PAGE_SIZE` ставится по ней. На здоровом пути видно 64 и
#: больше, и выгрузка марки сворачивается с тридцати запросов до двух-трёх.
#:
#: Восьмёрка ещё и делится пополам до единицы без остатка, а это условие точности
#: спасателя (`_rescue`): половины страницы адресуются той же арифметикой страниц, и на
#: нечётном размере границы разъехались бы. Поднимать — только с цифрами от
#: `tests.integration_onec_health`, он для того и меряет лестницу размеров.
PAGE_SIZE = 8

#: Со скольких секунд вызов 1С считается медленным и попадает в журнал. Лёгкие эндпоинты
#: отвечают за полсекунды, так что три секунды — это уже «идёт что-то тяжёлое».
SLOW_CALL_SECONDS = 3.0

#: Сколько ждать СТРАНИЦУ выгрузки и сколько — ОДНУ позицию.
#:
#: **Ответ здесь либо быстрый, либо никакой.** Замер на бою 24.09.2026: страница из 8
#: позиций — 1,4 с, из 25 — 3,3 с, одна позиция — 1,2 с; а потерянный запрос не приходит
#: ВООБЩЕ, сколько его ни жди. Поэтому щедрость предела не покупает ничего: 45 секунд
#: ожидания в боевом логе стоили по полторы минуты на каждую сорвавшуюся страницу
#: (попытка плюс повтор) при цене здоровой в полторы секунды.
#:
#: Двенадцать — это в восемь раз больше самого медленного виденного ответа, так что
#: честно медленную страницу мы не обрываем. Разница в рисках: обрыв СТРАНИЦЫ ничего не
#: теряет — её достанет деление пополам; обрыв ОДНОЙ позиции объявляет её недоступной,
#: и потому там предел вдвое щедрее.
PAGE_TIMEOUT = 12.0
ITEM_TIMEOUT = 20.0


def _pointless_to_repeat(exc: Exception) -> bool:
    """Ждать дольше бессмысленно: сервер ПРИНЯЛ запрос и не ответил вовремя.

    `ConnectTimeout` сюда не входит: соединение не установилось, сервер о нас не знает,
    и повтор — ровно то, что нужно (`WinError 10060`).
    """
    return (isinstance(exc, httpx.TimeoutException)
            and not isinstance(exc, httpx.ConnectTimeout))


class OnecClient:
    """Синхронный клиент 1С. base_url — до /api_shop/hs/ai-tools (без хвостового /)."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0,
                 retries: int = 5, backoff: float = 2.0) -> None:
        self._retries = max(1, retries)
        # Пауза между повторами растёт линейно. Вынесена параметром ради тестов: ждать
        # двадцать секунд на каждый случай обрыва — это минута к прогону набора.
        self._backoff = max(0.0, backoff)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Token": token},
            timeout=timeout,
            # СОЕДИНЕНИЕ НЕ ПЕРЕИСПОЛЬЗУЕТСЯ, и это главный вывод разбора 24.09.2026.
            #
            # Замер на боевой публикации (один узел, Microsoft-IIS/10.0): по одному и
            # тому же соединению каждый ТРЕТИЙ запрос уходит в никуда — №3, №6, №9, №12
            # не отвечают вовсе при норме 0,2 с, — а новым соединением на каждый запрос
            # проходят 9 из 9 за 0,4–0,5 с. То же самое на выгрузке номенклатуры, только
            # там соединение умирает позже: гибнет каждый тринадцатый.
            #
            # Причина в том, что сервер закрывает keep-alive со своей стороны МОЛЧА: наш
            # сокет остаётся живым, запрос уходит в него — и ответа не будет никогда,
            # только наш таймаут. Иногда тот же обрыв приходит честным `WinError 10054`
            # (`httpx.ReadError`) — его лечит повтор в `_retry`, — но тихий вариант
            # повтором не лечится, потому что мы о нём узнаём лишь через минуты.
            #
            # Цена отказа от переиспользования — рукопожатие на запрос, здесь 0,2 с.
            # Против запроса, потерянного на 45 секунд, это даром.
            limits=httpx.Limits(max_keepalive_connections=0, max_connections=10),
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict | None = None,
             timeout: float | None = None) -> httpx.Response:
        """GET с повторами. Сеть до 1С рвётся по двум разным поводам, и оба штатные.

        `timeout` задаётся там, где известна цена ответа: выгрузка номенклатуры знает,
        что здоровая страница приходит за секунды, и ждать её две минуты незачем.
        """
        kw = {} if timeout is None else {"timeout": timeout}
        return self._retry(lambda: self._client.get(path, params=params, **kw), path)

    def _retry(self, call, label: str = ""):
        """Повторить запрос, если оборвалась СВЯЗЬ. Медленный ответ не повторяется.

        **Ловится `TransportError`, а не три отдельных исключения.** Поводов два, и второй
        нашёлся только на длинном прогоне:

          * `WinError 10060` — сервис не принимает соединение, было известно давно;
          * `WinError 10054` (`httpx.ReadError`) — сервер РВЁТ простаивающее keep-alive
            соединение, и следующий запрос уходит в уже мёртвый сокет. Провайдер модели
            опрашивает 1С раз в 5 секунд бесконечно, так что это не редкость, а
            расписание: клиент живёт часами, а IIS закрывает неиспользуемые соединения
            по своему таймауту.

        Повтор здесь и лечит: httpx открывает новое соединение взамен закрытого.
        `TransportError` — общий предок всех сетевых сбоев httpx, и перечислять их
        поимённо значит ждать следующего забытого.

        `HTTPStatusError` сюда НЕ попадает: 500 от 1С это ответ, а не обрыв, и повторять
        его бессмысленно — приедет тот же самый.

        **ТАЙМАУТ ЧТЕНИЯ НЕ ПОВТОРЯЕТСЯ.** Он значит, что сервер запрос ПРИНЯЛ и думает, а
        1С не отменяет работу, когда клиент отвалился: брошенная выгрузка продолжает
        крутиться. Повторяя её пять раз, мы кладём на базу пять тяжёлых запросов вместо
        одного и сами превращаем медленный ответ в неотвечающий (бой 24.09.2026: агент
        молчал десять минут, пока `by-tm` не отвечал). Обрыв соединения — другое дело: там
        сервер ничего не делает, и повтор ровно лечит.

        Каждый вызов, занявший больше `SLOW_CALL_SECONDS`, попадает в журнал: десять минут
        тишины должны читаться как «идёт `by-tm` по такой-то марке», а не как «всё зависло».
        """
        last: Exception | None = None
        for attempt in range(self._retries):
            started = time.monotonic()
            try:
                answer = call()
                spent = time.monotonic() - started
                if spent >= SLOW_CALL_SECONDS:
                    logger.info("1С отвечала %.1f с: %s", spent, label or "запрос")
                return answer
            except httpx.TransportError as exc:
                spent = time.monotonic() - started
                if _pointless_to_repeat(exc):
                    logger.warning(
                        "1С не ответила за %.0f с (%s): %s — повтор только добавил бы ей "
                        "работы, сдаюсь", spent, type(exc).__name__, label or "запрос")
                    raise
                last = exc
                logger.warning("Связь с 1С оборвалась (%s) на %s — попытка %d из %d",
                               type(exc).__name__, label or "запрос",
                               attempt + 1, self._retries)
                if self._backoff:
                    time.sleep(self._backoff * (attempt + 1))
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
              product_type: str | None = None,
              timeout: float | None = None) -> NomenclaturePage:
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
        r = self._get("/get-products/by-tm", params=params, timeout=timeout)
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

    def find_items(self, article: str = "", name: str = "", tm: str | None = None,
                   limit: int = 50, articles: list[str] | None = None) -> FoundItems:
        """Поиск позиций ПО ВСЕЙ номенклатуре — по артикулам и/или имени (§19.11).

        Нужен ровно для одного: перед созданием позиции убедиться, что её нет среди снятых
        с производства. Проверка по `by_tm` этого не даёт — она видит одну марку, а товар
        мог быть заведён под другой, и именно такой дубль проверка и должна ловить.

        **Спрашивать надо ПАЧКОЙ.** Цена определяется не объёмом ответа, а числом вызовов:
        цикл агента ручной, и каждый вызов инструмента — отдельный запрос к модели со всей
        историей ($0.097 на боевом прогоне против $0.0002 за сам ответ). Проверка по одной
        позиции стоила бы ~$5 на прайс, пачкой по коллекции — ~$0.77.

        Невыгружаемые (а снятые лежат именно там) приходят ПО УМОЛЧАНИЮ — в отличие от
        `by_tm`, где их надо просить отдельно.
        """
        params: dict = {"limit": limit}
        batch = [a.strip() for a in (articles or []) if a and a.strip()]
        if batch:
            params["articles"] = ",".join(batch)
        if article:
            params["article"] = article
        if name:
            params["name"] = name
        if tm:
            params["tm"] = tm
        r = self._get("/get-products/find-items", params=params)
        r.raise_for_status()
        data = _loads_bom(r.content)
        items = [
            FoundItem(
                ref=str(x.get("ref", "")),
                name=(x.get("name") or "").strip(),
                full_name=(x.get("full_name") or "").strip(),
                article=(x.get("article") or "").strip(),
                unit=(x.get("unit") or "").strip(),
                product_type=(x.get("product_type") or "").strip(),
                product_type_ref=str(x.get("product_type_ref", "")),
                tm=(x.get("tm") or "").strip(),
                tm_code=str(x.get("tm_code", "")),
                parent_ref=str((x.get("parent") or {}).get("code", "")),
                parent_name=((x.get("parent") or {}).get("name") or "").strip(),
                not_exported=bool(x.get("not_exported", False)),
            )
            for x in (data.get("items") or [])
        ]
        return FoundItems(
            items=items,
            total=int(data.get("total", len(items)) or 0),
            truncated=bool(data.get("truncated", False)),
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

    # ------------------------------------------------- обмен с формой модели (§3)
    #
    # Три метода ниже обслуживают ВТОРОЙ ВИЗУАЛ — форму 1С (specs/1c-model-form.md).
    # Обе стрелки идут ОТ агента: обработчик HTTP-сервиса работает в своём сеансе и до
    # открытой формы не дотягивается, толкнуть данные в неё платформа не даёт.
    #
    # `_post_json` общий: у трёх маршрутов одна обвязка — тело в UTF-8, проверка на
    # HTML-страницу веб-сервера вместо JSON и разбор ответа.

    def _post_json(self, path: str, payload: dict, timeout: float = 60) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        # Повторы обязательны и здесь, а не только у GET: провайдер зовёт эти маршруты в
        # том же бесконечном цикле, и оборванное keep-alive соединение достаётся тому
        # запросу, который случился первым, — POST ничем не защищённее GET.
        #
        # Повтор БЕЗОПАСЕН, потому что оба POST идемпотентны по построению:
        # `set-model-state` кладёт полный снимок (повтор даст `changed = 0`), а
        # `agent-commands-state` двигает состояние команды только вперёд и на повторное
        # сообщение отвечает `already_final`.
        r = self._retry(lambda: self._client.post(
            path, content=body, headers=JSON_UTF8, timeout=timeout), path)
        text = r.content.decode("utf-8-sig", errors="replace")
        # Необработанное исключение BSL веб-сервер подменяет своей страницей: текста 1С в
        # ней нет вовсе, и без этой проверки мы бы разбирали HTML как JSON.
        if "<!DOCTYPE" in text or "<html" in text.lower():
            raise RuntimeError(f"1С вернул HTML вместо JSON (HTTP {r.status_code})")
        r.raise_for_status()
        return json.loads(text)

    def agent_commands(self) -> dict:
        """Забрать ждущие команды формы вместе с часами 1С (§3.1).

        `server_time` в ответе обязателен: по нему считается смещение часов. Порядок «кто
        первый» решается временем создания на стороне визуала, и сбитые часы давали бы 1С
        либо вечный выигрыш, либо вечный проигрыш.

        Эндпоинт лёгкий — читает только таблицу команд. Замер: 231 мс против 1 263 мс у
        запроса с обращением к Номенклатуре, то есть 4,6 % против 25,3 % занятости 1С при
        опросе раз в пять секунд.
        """
        r = self._get("/get-products/agent-commands")
        text = r.content.decode("utf-8-sig", errors="replace")
        if "<!DOCTYPE" in text or "<html" in text.lower():
            raise RuntimeError(f"1С вернул HTML вместо JSON (HTTP {r.status_code})")
        r.raise_for_status()
        return json.loads(text)

    def agent_commands_state(self, items: list[dict]) -> dict:
        """Сообщить судьбу команд: `принята`, затем `выполнена` либо `отклонена` (§3.2).

        Зовётся ДВАЖДЫ на команду намеренно. Между «забрал» и «применил» агент может
        умереть, и тогда команда обязана остаться в работе; а форме нужны оба момента —
        колесико ожидания гаснет по РЕЗУЛЬТАТУ, а не по факту, что команду забрали.
        """
        return self._post_json("/get-products/agent-commands-state", {"commands": items})

    def set_model_state(self, prices: list[dict]) -> dict:
        """Положить в 1С ПОЛНЫЙ снимок состояния модели (§3.3).

        Целиком, а не приращениями: снимок мал (десятки строк) и самоисцеляющийся —
        потерянное обновление чинится следующим, тогда как с приращениями расхождение
        копилось бы молча.

        РАЗНИЦУ СЧИТАЕТ 1С: она трогает только изменившиеся строки и поднимает счётчик
        версии, лишь если разница непустая. Иначе форма перерисовывалась бы каждые пять
        секунд под руками у админа.

        Отсутствующий ключ `prices` и пустой список — РАЗНОЕ: пустой честно вычищает
        зеркало, отсутствующий 1С отвергает, чтобы обрезанный запрос не стёр список.
        """
        return self._post_json("/get-products/set-model-state", {"prices": prices})

    def by_tm_all(self, tm_code: str, size: int = PAGE_SIZE, max_pages: int = 80,
                  include_not_exported: bool = False,
                  product_type: str | None = None) -> Nomenclature:
        """Все страницы номенклатуры ТМ вместе с ошибками отдельных позиций.

        **ЦЕНУ ОПРЕДЕЛЯЕТ ЧИСЛО ЗАПРОСОВ, А НЕ РАЗМЕР СТРАНИЦЫ.** Замер 24.09.2026 на
        боевой базе: страница из 1 позиции — 1,2 с, из 5 — 1,1 с, из 10 — 1,2 с. Платим
        за запрос по марке, позиции внутри стоят сотые доли секунды. Поэтому дробить
        мелко нечего: это не бережёт базу, а множит поводы потерять запрос.

        **НЕ ОТДАЛАСЬ — ДЕЛИМ ПОПОЛАМ**, а не разбираем по одной. Разбор по одной стоил
        25 запросов и полминуты на каждую сорвавшуюся страницу, и этим сам нагружал 1С,
        из-за чего срывалась следующая. Деление пополам обходится двумя запросами, когда
        провал случайный (а он случайный: соседние позиции отвечают за секунду), и
        доходит до одной позиции только там, где не отдаётся именно она.
        """
        # Предел ожидания СЧИТАЕТСЯ ПО РАЗМЕРУ запроса (`_ask_page` и `_ask_item`), а не хранится
        # здесь: у половины размера 1 он обязан быть коротким, где бы она ни запрашивалась.
        kw = {"include_not_exported": include_not_exported, "product_type": product_type}
        items: list = []
        errors: list = []
        tm_name = ""
        total: int | None = None

        for page in range(1, max_pages + 1):
            try:
                chunk = self._ask_page(tm_code, page, size, kw)
            except httpx.TimeoutException:
                # Страницу не теряем целиком: делим её пополам и спускаемся только в ту
                # половину, которая не отдалась.
                logger.warning("Страница %d марки %s не отдалась — делим пополам",
                               page, tm_code)
                seen = self._rescue(tm_code, page, size, kw, items, errors)
                if seen is not None:
                    total = seen
            else:
                tm_name = tm_name or (chunk.tm or "")
                total = chunk.total
                items.extend(chunk.items)
                errors.extend(chunk.errors)

            # Размер марки известен только из ответа: не ответила НИ ОДНА позиция
            # страницы — продолжать вслепую нечем.
            if total is None or page * size >= total:
                break

        return Nomenclature(tm=tm_name or tm_code, total=total or len(items), items=items,
                            errors=errors)

    def _ask_page(self, tm_code: str, page: int, size: int, kw: dict):
        """Спросить страницу ОДИН раз. Повтора здесь нет намеренно.

        Повторять страницу целиком незачем: провал транзиентный, а половины той же
        страницы — это и повторная попытка, и полезная работа сразу. Повтор же стоил
        второго полного ожидания: в боевом логе сорвавшаяся страница обходилась в
        полторы минуты (45 с попытка + 45 с повтор) при цене здоровой в полторы секунды.

        Половина размера 1 — уже не страница, и ждём её по правилам позиции: обрыв
        страницы ничего не теряет, а обрыв позиции ведёт к тому, что её объявят
        недоступной.
        """
        wait = ITEM_TIMEOUT if size <= 1 else PAGE_TIMEOUT
        return self.by_tm(tm_code, page=page, size=size, timeout=wait, **kw)

    def _ask_item(self, tm_code: str, index: int, kw: dict):
        """Спросить ОДНУ позицию, с единственной повторной попыткой.

        Вот здесь повтор нужен, и он последний: дальше делить нечего, и не ответившая
        позиция будет объявлена недоступной. Замер 24.09.2026: тридцать одинаковых
        запросов подряд по одной позиции — №13 и №26 не ответили вовсе, остальные за
        1,3 с. Одна такая случайность не должна стоить позиции в выгрузке.
        """
        try:
            return self.by_tm(tm_code, page=index, size=1, timeout=ITEM_TIMEOUT, **kw)
        except httpx.TimeoutException:
            logger.info("Позиция №%d марки %s не ответила — пробуем ещё раз",
                        index, tm_code)
            return self.by_tm(tm_code, page=index, size=1, timeout=ITEM_TIMEOUT, **kw)

    def _rescue(self, tm_code: str, page: int, size: int, kw: dict,
                items: list, errors: list) -> int | None:
        """Достать сорвавшуюся страницу делением пополам. Возвращает «всего у марки».

        Половины адресуются ТОЙ ЖЕ арифметикой страниц: страница `page` размера `size` —
        это позиции с `(page-1)*size + 1` по `page*size`, а её половины при чётном размере
        это ровно страницы `2*page-1` и `2*page` размера `size/2`. Поэтому деление точное,
        пока размер делится; на нечётном размере спускаемся по одной позиции — иначе
        границы разъедутся и позиция потеряется молча.

        Зачем вообще делить, а не спрашивать по одной: провал СЛУЧАЕН (соседние позиции
        отвечают за секунду), и половины проходят с первой же попытки. Разбор по одной
        стоил 25 запросов и полминуты на страницу — и этой нагрузкой сам ронял следующую.

        Позиция, не ответившая в одиночку, становится ОШИБКОЙ в выгрузке — такой же, какие
        обработчик 1С отдаёт по сбойным карточкам сам. Молча пропустить её нельзя: пропажа
        позиции читается агентом как «товара в 1С нет», а это прямой путь к дублю.
        """
        if size <= 1:
            index = page                                # размер 1 — номер позиции
            try:
                one = self._ask_item(tm_code, index, kw)
            except httpx.TimeoutException:
                errors.append({
                    "ref": "", "code": "item_timeout",
                    "message": f"позиция №{index} марки не отдаётся: 1С молчит. "
                               f"Карточка стоит между соседними по коду номенклатуры",
                })
                return None
            items.extend(one.items)
            return one.total

        if size % 2:
            # Нечётный размер пополам не делится без сдвига границ — идём по одной.
            first = (page - 1) * size + 1
            total = None
            for index in range(first, first + size):
                seen = self._rescue(tm_code, index, 1, kw, items, errors)
                if seen is not None:
                    total = seen
                    if index >= seen:
                        break
            return total

        half = size // 2
        total = None
        for part in (page * 2 - 1, page * 2):
            # За концом марки половина пустая — это не ошибка, а край.
            if total is not None and (part - 1) * half >= total:
                break
            try:
                chunk = self._ask_page(tm_code, part, half, kw)
            except httpx.TimeoutException:
                seen = self._rescue(tm_code, part, half, kw, items, errors)
            else:
                items.extend(chunk.items)
                errors.extend(chunk.errors)
                seen = chunk.total
            if seen is not None:
                total = seen
        return total
