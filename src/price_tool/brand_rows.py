"""Чей это бренд — для КАЖДОЙ строки листа, пятью способами (решение админа 04.10.2026).

**ЗАЧЕМ ЕДИНЫЙ ВХОД.** Выбор брендов работал только там, где бренд выделен КОЛОНКОЙ, то есть
у одного формата из пяти. А прайс Артисаны — один лист на 12 870 строк ≈ **316 тыс. токенов**,
и бренд в нём обозначен ГОРИЗОНТАЛЬНЫМ РАЗДЕЛИТЕЛЕМ: «Amadis Fine Tiles, S.A.», «APE»,
«Atlas Concorde ( Italy)». Из 38 брендов магазину нужны единицы: APE с Amadis — это 399
токенов вместо 316 440. Бывает и третий способ — баннер картинкой (так у A+ Floor).

**ПО СТРОКАМ ИДЁТ КОД, А НЕ МОДЕЛЬ.** Пройди по ним модель — она бы за них уже заплатила:
чтение и есть расход. Поэтому здесь смотрится ОДНА ячейка на строку, решается «чей бренд», и
только отмеченные строки собираются обратно в `Sheet` для показа модели.

**ПРОМАХ ДЕТЕКТОРА НЕ ИМЕЕТ ПРАВА СТАТЬ НЕВЕРНЫМ СПИСКОМ.** Правило «одинокая ячейка =
раздел» на боевых мелких форматах даёт мусор, и это ЗАМЕРЕНО: у Most Floor одинокими выглядят
ТОВАРЫ (86 строк из 107 — цены там стоят одной строкой на коллекцию), у Линдервуда верхним
уровнем становится примечание «Важно: цены включают в себя стоимость доставки…», у FLOOR
SERVICE — обрывки в колонке 26. Список «брендов» из товаров ХУЖЕ отсутствующего: по нему
РЕЖУТ файл. Поэтому у разделителей есть порог доказательств, а не сошлось — возвращаем None,
и поведение остаётся прежним.

Модуль чистый: ни базы, ни сети, ни 1С.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.price_tool.brands import BrandColumn, _effective, find_brand_column
from src.price_tool.parser import Sheet, non_empty_rows
from src.price_tool.scope import normalize

#: Способ, которым бренд обозначен в этом листе. Хранится у сигнатуры: список брендов и
#: фильтр строк обязаны считаться одинаково и на приёме, и при разборе.
#: ВЕРСИЯ ПРАВИЛА ДЕТЕКЦИИ. Поднимается КАЖДЫЙ раз, когда детекторы начинают видеть больше
#: прежних, и по ней дозаполнение понимает, какие форматы перечитать (`brand_backfill`).
#:
#: Без неё каждое улучшение молча обходило бы уже просмотренные форматы — это случилось
#: дважды за один день: сперва у FLOOR SERVICE остался пустой способ, потом Стройиндустрия
#: не перечиталась новыми детекторами, потому что способ у неё уже стоял. Признак «смотрели»
#: обязан говорить, ЧЕМ смотрели.
#:
#:   1 — только колонка бренда;
#:   2 — плюс разделители по размеру, картинки-баннеры, разделители по справочнику марок и
#:       бренд в имени листа (04.10.2026);
#:   3 — картинки СТАРОГО `.xls` (`price_tool.xls_images`) и правило «логотип выше строки
#:       заголовков — это шапка, а не разделитель» (04.10.2026, прайс Линдервуда).
RULE_VERSION = 3

BY_COLUMN = "колонка"
BY_SECTION = "разделитель"
BY_MARK_SECTION = "разделитель по справочнику"
BY_SHEET_NAME = "имя листа"
BY_IMAGE = "картинка"
#: «Смотрели файл — бренд не обозначен никак». Это ОТВЕТ, и он записывается наравне с
#: остальными: пустой способ у формата означает другое — «смотрели правилом прежней
#: версии», и такой формат надо перечитать (см. `brand_backfill`).
BY_NONE = "нет"

#: Порог доказательств для РАЗДЕЛИТЕЛЕЙ. Числа из замера на пяти боевых форматах: у них
#: 44–220 строк на лист, и там правило врёт; у Артисаны 12 870 — и там оно точно
#: восстанавливает дерево (38 брендов, 1105 коллекций, 11 720 товарных строк).
SECTION_MIN_ROWS = 1000
#: Разделов верхнего уровня должно быть несколько: два-три раздела — это скорее примечания
#: («ПОДЛОЖКА ЛИСТОВАЯ 3 мм» и «Важно: …» у Линдервуда), чем перечень брендов.
SECTION_MIN_BRANDS = 5
#: Строка товара — это строка С ДАННЫМИ, в ней заполнено несколько ячеек. То же число, что у
#: шапки сигнатуры (`signature.HEAD_MIN_CELLS`): там оно отделяет заголовок от оформления.
ITEM_MIN_CELLS = 3
#: Товарных строк обязано быть кратно больше, чем разделов. У Артисаны 11 720 против 1143.
SECTION_ITEM_RATIO = 3


@dataclass(frozen=True)
class BrandRows:
    """Кто владеет каждой строкой листа.

    `rows` — ВСЕ строки, принадлежащие бренду: и товарные, и его собственные строки-разделители
    вместе со строками коллекций. Последние обязаны уехать модели вместе с товарами: задачи
    адресуются ПАРОЙ (марка, коллекция), и без строки «Boost Natural» модель не узнает, к какой
    коллекции относится позиция.

    `items` — какие из них товарные. По ним считается «строк у бренда»: число разделителей
    админу неинтересно, а завышенный счёт сделал бы бесполезной оценку расхода.
    """
    mode: str
    header_rows: int
    rows: tuple[tuple[int, str], ...]
    items: frozenset[int]
    column: int | None = None

    @property
    def brands(self) -> list[str]:
        """Бренды в порядке файла, по одному разу. Имена — КАК НАПИСАНЫ."""
        seen: set[str] = set()
        out: list[str] = []
        for _, brand in self.rows:
            key = normalize(brand)
            if key and key not in seen:
                seen.add(key)
                out.append(brand)
        return out


def brand_map(sheet: Sheet, images=None, marks=None) -> BrandRows | None:
    """Как в этом листе обозначен бренд. None — никак, выбор брендов недоступен.

    Порядок попыток — ПО УБЫВАНИЮ ДОКАЗАТЕЛЬНОЙ СИЛЫ, и он не произволен:

    1. **колонка** — признак стоит в КАЖДОЙ строке, гадать не о чем;
    2. **картинка** — код знает, ГДЕ баннер, имя на нём уже прочитано моделью (`images` —
       готовое «строка → бренд», см. `model/logo_intake.py`);
    3. **разделитель по размеру** — доказательство в самой раскладке: лист крупный, разделов
       много, товарных строк кратно больше (`find_sections`);
    4. **разделитель по справочнику ТМ** — доказательство ВНЕШНЕЕ: текст строки совпал с
       именем настоящей марки 1С (`find_marked_sections`). Так размечен прайс Стройиндустрии:
       строка «CLASSEN», ниже её коллекции, затем строка «ULTRAFLOOR»;
    5. **имя листа** — последний рубеж: «Ассортимент CLASSEN» значит, что весь лист про эту
       марку (`from_sheet_name`).

    Почему справочник идёт ПОСЛЕ размера: внутри листа доказательство сильнее внешнего. И
    почему имя листа последним: на листе, названном по бренду, внутри может быть РАЗМЕТКА по
    другим брендам, и она главнее — иначе всё свалилось бы в одну марку.

    `marks` — имена марок 1С (`onec.selling_tm`), нужны шагам 4 и 5. Без них работают только
    первые три: выдумывать бренды, не сверяясь ни с чем, мы не станем.
    """
    spot = find_brand_column(sheet)
    if spot is not None:
        return _from_column(sheet, spot)

    by_image = _from_images(sheet, images)
    if by_image is not None:
        return by_image

    by_size = find_sections(sheet)
    if by_size is not None:
        return by_size

    keys = mark_keys(marks)
    return find_marked_sections(sheet, keys) or from_sheet_name(sheet, keys)


def name_keys(name: str) -> set[str]:
    """Имя марки по частям: «Classen / Классен» — два написания одной марки.

    В справочнике 1С двуязычная запись норма, и сравнивать поле целиком правильно везде,
    кроме сопоставления с чужим текстом: там совпадёт ровно одна половина.
    """
    text = str(name or "")
    out = {normalize(text)}
    out.update(normalize(part) for part in text.split("/"))
    return {key for key in out if key}


def mark_keys(marks) -> set[str]:
    """Имена марок 1С в нормализованном виде — то, с чем сверяется текст прайса."""
    keys: set[str] = set()
    for mark in marks or ():
        name = mark if isinstance(mark, str) else getattr(mark, "name", "")
        keys |= name_keys(name)
    return keys


def _from_column(sheet: Sheet, spot: BrandColumn) -> BrandRows:
    """Колонка: бренд в каждой строке, протяжка объединённых ячеек — в `brands._effective`."""
    rows = tuple(_effective(non_empty_rows(sheet), spot))
    return BrandRows(mode=BY_COLUMN, header_rows=spot.header_rows, rows=rows,
                     items=frozenset(number for number, _ in rows), column=spot.column)


def _cells(row) -> list[tuple[int, str]]:
    return [(index, str(cell).strip())
            for index, cell in enumerate(row) if str(cell or "").strip()]


def find_sections(sheet: Sheet) -> BrandRows | None:
    """Горизонтальные разделители: строка с ОДНОЙ ячейкой, уровень задаёт НОМЕР КОЛОНКИ.

    У Артисаны бренд стоит в колонке 0 («APE»), коллекция — в колонке 1 («Armonia»), товар —
    строка с кодом и данными. Самая левая колонка разделов и есть уровень бренда; всё глубже
    — коллекции, в список они не идут (решение админа: детализация до коллекций не нужна), но
    строки их СОХРАНЯЮТСЯ за брендом.

    **ШАПКА — ВСЁ ДО ПЕРВОГО БРЕНДА**, а не «строка заголовков»: у Артисаны их ДВЕ («Код |
    Заводской код | Вид…», ниже «Вид | Вид | Размер | м2…»), а выше ещё пять строк контактов
    и дата прайса. Отдав модели таблицу без второй строки заголовков, мы отняли бы у неё
    половину имён колонок.

    Порог доказательств — в константах модуля. Не сошлось — None.
    """
    rows = non_empty_rows(sheet)
    if len(rows) < SECTION_MIN_ROWS:
        return None

    # РАЗДЕЛЫ ИЩЕМ ТОЛЬКО НИЖЕ СТРОКИ ЗАГОЛОВКОВ. Над ней у Артисаны пять строк контактов, и
    # две из них — одиночные ячейки: «Прайс-лист на 26.08.2026» и «Позиции и цены, выделенные
    # красным цветом…». Первый прогон детектора записал вторую в БРЕНДЫ, а её товарными
    # строками стали сами заголовки таблицы (они идут сразу ниже и заполнены густо). Правило
    # то же, что у сигнатуры: заголовок — первая строка с несколькими ТЕКСТОВЫМИ ячейками.
    head_at = 0
    for number, row in enumerate(rows, 1):
        text = [value for _, value in _cells(row) if any(ch.isalpha() for ch in value)]
        if len(text) >= ITEM_MIN_CELLS:
            head_at = number
            break

    lonely: dict[int, list[int]] = {}
    items: list[int] = []
    for number, row in enumerate(rows, 1):
        if number <= head_at:
            continue
        cells = _cells(row)
        if len(cells) == 1:
            lonely.setdefault(cells[0][0], []).append(number)
        elif len(cells) >= ITEM_MIN_CELLS:
            items.append(number)

    if not lonely or len(items) < SECTION_MIN_BRANDS * SECTION_ITEM_RATIO:
        return None

    column = min(lonely)
    if len(lonely[column]) < SECTION_MIN_BRANDS:
        return None
    if len(items) < sum(len(v) for v in lonely.values()) * SECTION_ITEM_RATIO:
        return None

    first = min(lonely[column])
    deeper = {number for level, numbers in lonely.items() if level > column
              for number in numbers}

    # Проход по строкам: раздел верхнего уровня меняет бренд, более глубокий — коллекция
    # текущего бренда, остальное — товар. Строки ВЫШЕ первого бренда — шапка: там контакты и
    # заголовки, они нужны модели целиком.
    owned: list[tuple[int, str]] = []
    own_items: set[int] = set()
    counts: dict[str, int] = {}
    current = ""
    for number, row in enumerate(rows, 1):
        if number < first:
            continue
        cells = _cells(row)
        if len(cells) == 1 and cells[0][0] == column:
            current = cells[0][1]
            counts.setdefault(normalize(current), 0)
            owned.append((number, current))
            continue
        if not current:
            continue
        owned.append((number, current))
        if number not in deeper and len(cells) >= ITEM_MIN_CELLS:
            own_items.add(number)
            counts[normalize(current)] += 1

    # РАЗДЕЛ БЕЗ ТОВАРОВ — НЕ БРЕНД, а примечание: у Линдервуда так выглядит строка «Важно:
    # цены включают в себя стоимость доставки…». Выбрасываем такие вместе с их строками.
    empty = {key for key, rows_ in counts.items() if not rows_}
    if empty:
        owned = [(number, brand) for number, brand in owned
                 if normalize(brand) not in empty]
        own_items = {n for n, _ in owned} & own_items
    if len(counts) - len(empty) < SECTION_MIN_BRANDS:
        return None

    return BrandRows(mode=BY_SECTION, header_rows=first - 1, rows=tuple(owned),
                     items=frozenset(own_items), column=column)


def _head_at(rows: list[list[str]]) -> int:
    """Номер строки заголовков: первая с несколькими ТЕКСТОВЫМИ ячейками.

    Правило то же, что у сигнатуры формата (`signature._header`), и это не совпадение: обе
    задачи отличают заголовок от оформления, и расхождение двух правил вылезло бы молча.
    """
    for number, row in enumerate(rows, 1):
        text = [value for _, value in _cells(row) if any(ch.isalpha() for ch in value)]
        if len(text) >= ITEM_MIN_CELLS:
            return number
    return 0


def find_marked_sections(sheet: Sheet, keys: set[str]) -> BrandRows | None:
    """Разделители, опознанные ПО СПРАВОЧНИКУ МАРОК 1С. Для листа ЛЮБОГО размера.

    Так размечен прайс Стройиндустрии (44 строки — порог размера он не проходит и никогда не
    пройдёт): строка «CLASSEN», под ней её коллекции, затем строка «ULTRAFLOOR» и её. Обе
    есть в справочнике 1С («Classen / Классен», «Ultrafloor / Ультрафлор»), и это внешнее
    доказательство заменяет размер: совпадение с именем НАСТОЯЩЕЙ марки — не догадка.

    **ОДИНОКОЙ ЯЧЕЙКИ НЕ ТРЕБУЕМ** — и это главное отличие от `find_sections`. У
    Стройиндустрии в строке «ULTRAFLOOR» стоят ещё «цена 1» и «цена 2» в колонках 9 и 10
    (поставщик подписал там свои две цены), то есть заполнено ТРИ ячейки. Отличает разделитель
    от товара не число ячеек, а то, что в КОЛОНКАХ ДАННЫХ — тех, что заняты в строке
    заголовков, — у него пусто: у товара там замок, размер, класс и цены.

    **РАЗДЕЛ, НЕ СОВПАВШИЙ СО СПРАВОЧНИКОМ, БРЕНДОМ НЕ СЧИТАЕТСЯ.** В том же листе ниже стоит
    «Акссеуары» — это раздел подложек, а не марка; его строки достаются текущему бренду, и это
    осознанно: потерять их молча хуже, а лишние виды товара отсекают категории (`/categories`).
    """
    if not keys:
        return None

    rows = non_empty_rows(sheet)
    head_at = _head_at(rows)
    if not head_at:
        return None

    span = {index for index, _ in _cells(rows[head_at - 1])}

    owned: list[tuple[int, str]] = []
    own_items: set[int] = set()
    header = head_at
    current = ""
    for number, row in enumerate(rows, 1):
        if number <= head_at:
            continue
        cells = _cells(row)
        if not cells:
            continue
        first, text = cells[0]
        data = {index for index, _ in cells if index != first} & span
        if normalize(text) in keys and not data:
            current = text
            owned.append((number, current))
            continue
        if not current:
            header = number
            continue
        owned.append((number, current))
        own_items.add(number)

    if not own_items:
        return None
    return BrandRows(mode=BY_MARK_SECTION, header_rows=header, rows=tuple(owned),
                     items=frozenset(own_items))


def from_sheet_name(sheet: Sheet, keys: set[str]) -> BrandRows | None:
    """Бренд в ИМЕНИ ЛИСТА: «Ассортимент CLASSEN» — весь лист про эту марку.

    Последний рубеж, и потому самый строгий: имя листа сверяется со справочником 1С и ЦЕЛЫМ
    СЛОВОМ. «Ассортимент CLASSEN» совпадает, «Прайс от 01.10.2026» и «ИЗМЕНЕНИЯ» — нет.

    **ДВЕ МАРКИ В ИМЕНИ — ЗНАЧИТ НИ ОДНОЙ**: выбор наугад однажды припишет лист чужой марке, а
    по этому выбору потом отбираются строки и пишутся цены.

    **ТОВАРНЫМИ СЧИТАЮТСЯ ВСЕ строки листа ниже заголовков** (а не только густые): лист целиком
    принадлежит одной марке, и для оценки расхода важно, сколько строк уедет модели, — а уедут
    они все. У Стройиндустрии на таком листе одинокими ячейками размечены КОЛЛЕКЦИИ («Elegant
    4V»), и брендами они не становятся именно потому, что имя листа сильнее.
    """
    if not keys:
        return None

    words = set(normalize(sheet.name).split())
    hits = {key for key in keys if key and set(key.split()) <= words}
    if len(hits) != 1:
        return None

    # Имя показываем КАК НАПИСАНО В ЛИСТЕ — так админ узнает в списке свой прайс. Берём то
    # слово имени листа, которым совпали: «Ассортимент CLASSEN» → «CLASSEN».
    key = next(iter(hits))
    shown = next((word for word in str(sheet.name).split()
                  if normalize(word) in key.split()), key)

    rows = non_empty_rows(sheet)
    head_at = _head_at(rows)
    owned = [(number, shown) for number in range(head_at + 1, len(rows) + 1)]
    if not owned:
        return None
    return BrandRows(mode=BY_SHEET_NAME, header_rows=head_at, rows=tuple(owned),
                     items=frozenset(number for number, _ in owned))


def _raw_header_row(sheet: Sheet) -> int:
    """Номер СЫРОЙ строки заголовков (0 — заголовков нет).

    Якоря картинок приходят в сырой нумерации книги, а `_head_at` считает по непустым —
    сравнивать их напрямую нельзя.
    """
    head = _head_at(non_empty_rows(sheet))
    if not head:
        return 0
    seen = 0
    for raw, row in enumerate(sheet.rows, 1):
        if any(cell for cell in row):
            seen += 1
            if seen == head:
                return raw
    return 0


def _from_images(sheet: Sheet, images) -> BrandRows | None:
    """Баннеры картинками: `images` = {имя листа: [(строка, бренд)]} либо {строка: бренд}.

    Соответствие «строка → бренд» готовит вызывающий: код знает, ГДЕ лежит картинка
    (`parser.image_anchor_rows`), а ЧЕЙ на ней логотип — читает модель один раз на логотип и
    помнит по его хешу. Здесь остаётся механическая часть: бренд действует от своей строки до
    следующей.

    **НУЖНЫ ДВА РАЗНЫХ ЯКОРЯ, иначе разделять нечего** — и это не осторожность, а замер по
    боевым файлам (04.10.2026): у Монарха 93 картинки на листе привязаны ВСЕ к строке 1 (это
    плавающие фото товаров), у FLOOR SERVICE и Most Floor по одной на лист — шапка. Один
    якорь означал бы «весь лист — один бренд», то есть список из одной записи и ложное
    чувство, что выбор работает.
    """
    anchors = _anchors_for(sheet, images)

    # ЛОГОТИП ВЫШЕ СТРОКИ ЗАГОЛОВКОВ — ЭТО ШАПКА, А НЕ РАЗДЕЛИТЕЛЬ. У Линдервуда в самом
    # верху стоит логотип поставщика, и приняв его за начало блока, мы отдали бы ему строку
    # с названиями колонок: выбери админ другой бренд — и модель получила бы таблицу без
    # заголовков (04.10.2026). Заодно это отсекает форматы с одной картинкой-шапкой на лист
    # (FLOOR SERVICE, Most Floor): разделять там нечего.
    head = _raw_header_row(sheet)
    anchors = {row: brand for row, brand in anchors.items() if row > head}

    if len(anchors) < 2:
        return None

    # ЯКОРЬ — СЫРОЙ НОМЕР СТРОКИ, А СЧЁТ ИДЁТ ПО НЕПУСТЫМ. `image_anchor_rows` отдаёт
    # номер строки В КНИГЕ (как и `mark_images`, который по нему вставляет маркеры), а
    # `rows`/`items` обязаны быть в нумерации `non_empty_rows` — по ней работают и фильтр, и
    # чтение. Строка под баннером чаще всего ПУСТАЯ (картинка плавает над листом), так что
    # сравнивать одно с другим напрямую значит сдвинуть бренды на число пустых строк выше.
    owned: list[tuple[int, str]] = []
    own_items: set[int] = set()
    header = 0
    seen = 0
    current = ""
    for raw, row in enumerate(sheet.rows, 1):
        filled = any(cell for cell in row)
        if filled:
            seen += 1
        if raw in anchors:
            current = anchors[raw]
        if not current:
            header = seen
            continue
        if not filled:
            continue
        owned.append((seen, current))
        # СТРОКА С ЯКОРЕМ БЫВАЕТ И ТОВАРНОЙ. Выбросив её как «разделитель», мы потеряли бы
        # позицию: бренд начинается С НЕЁ (поймано своим же тестом 04.10.2026).
        if len(_cells(row)) >= ITEM_MIN_CELLS:
            own_items.add(seen)

    if not own_items:
        return None
    return BrandRows(mode=BY_IMAGE, header_rows=header, rows=tuple(owned),
                     items=frozenset(own_items))


def _anchors_for(sheet: Sheet, images) -> dict[int, str]:
    if not images:
        return {}
    found = images.get(sheet.name, images) if isinstance(images, dict) else images
    if isinstance(found, dict):
        pairs = found.items()
    else:
        pairs = found or ()
    out: dict[int, str] = {}
    for number, brand in pairs:
        try:
            line = int(number)
        except (TypeError, ValueError):
            continue
        name = str(brand or "").strip()
        if line > 0 and name:
            out[line] = name
    return out


def brands_in_rows(spot: BrandRows) -> list[tuple[str, int]]:
    """Бренды листа с числом ТОВАРНЫХ строк, в порядке файла.

    Порядок не алфавитный намеренно: админ ищет бренд глазами там, где он стоит в книге, — по
    той же причине, по которой листы в форме идут порядком вкладок.
    """
    counts: dict[str, int] = {}
    for number, brand in spot.rows:
        key = normalize(brand)
        counts.setdefault(key, 0)
        if number in spot.items:
            counts[key] += 1
    return [(brand, counts[normalize(brand)]) for brand in spot.brands]


def brand_per_raw_row(sheet: Sheet, spot: BrandRows) -> list[str]:
    """Бренд для КАЖДОЙ строки `sheet.rows`, по порядку. Пустая строка — бренда нет.

    Нужен тем, кто ходит по СЫРЫМ строкам листа, а не по непустым: так читает цены
    `price_check.prices_from_rows`, и скидка у неё спрашивается построчно (у каждого бренда
    своя). Протяжка объединённых ячеек тут уже учтена — она сделана в `rows`.
    """
    owner = {number: brand for number, brand in spot.rows}
    out: list[str] = []
    seen = 0
    for row in sheet.rows:
        if any(cell for cell in row):
            seen += 1
            out.append(owner.get(seen, ""))
        else:
            out.append("")
    return out


def only_brand_rows(sheet: Sheet, spot: BrandRows, wanted) -> Sheet:
    """Лист из ШАПКИ и всех строк отмеченных брендов (вместе с их разделителями).

    Отдаём `Sheet`, а не текст: дальше его показывает `render_preview`, и вся механика чтения
    — нумерация строк, листание `from_row`, пометка «ещё N строк не показано» — остаётся
    прежней. Нумерация у отфильтрованного листа СВОЯ, от единицы: `from_row` считается по
    тому, что агент видит.

    Пустой `wanted` даёт лист из одной шапки: это законный исход («ни один бренд не
    отмечен»), и обрабатывает его вызывающий, а не мы молча.
    """
    keys = {normalize(name) for name in (wanted or ()) if normalize(name)}
    rows = non_empty_rows(sheet)

    kept = list(rows[:spot.header_rows])
    for number, brand in spot.rows:
        if normalize(brand) in keys and 0 < number <= len(rows):
            kept.append(rows[number - 1])

    return Sheet(name=sheet.name, rows=kept)


def rows_cost(sheet: Sheet, spot: BrandRows, wanted) -> tuple[int, int]:
    """Сколько ТОВАРНЫХ строк и знаков уедет модели при таком выборе.

    Знаки нужны ради оценки в токенах: без детализации до коллекций отметка бренда — это всё
    или ничего, и Kerama у Артисаны это 6 702 строки ≈ 200 тыс. токенов. Молчаливый
    перерасход — ровно то, против чего заводится весь механизм.
    """
    keys = {normalize(name) for name in (wanted or ()) if normalize(name)}
    rows = non_empty_rows(sheet)
    lines = chars = 0
    for number, brand in spot.rows:
        if normalize(brand) not in keys or not 0 < number <= len(rows):
            continue
        if number in spot.items:
            lines += 1
        chars += len("\t".join(str(cell or "") for cell in rows[number - 1]))
    return lines, chars
