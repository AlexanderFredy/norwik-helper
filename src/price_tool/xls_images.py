"""Картинки и их строки из СТАРОГО `.xls` (BIFF8) — то, чего не умеет openpyxl.

**ЗАЧЕМ.** Бренд в прайсе бывает обозначен баннером, и у Линдервуда это именно так: логотип
«Peli®» во всю ширину на строке 5, логотип «LINDERWOOD» на строке 63 — между ними ламинат
Peli, под ними кварцвинил. Текстом этого в файле нет вообще. А `parser.image_anchor_rows`
работает через openpyxl, который `.xls` не открывает вовсе, — и выбор брендов у таких
форматов был слеп (04.10.2026: сперва я решил, что картинки в `.xls` недоступны, и ошибся).

**КАК УСТРОЕН `.xls`.** Это OLE2-контейнер; таблица лежит в потоке `Workbook` записями BIFF
(тип, длина, данные). Рисунки размазаны по двум местам:

* `MSODRAWINGGROUP` (0x00EB) в глобальном подпотоке — САМИ КАРТИНКИ, записями `BSE` (0xF007),
  внутри каждой лежит блип с байтами PNG/JPEG;
* `MSODRAWING` (0x00EC) в подпотоке ЛИСТА — фигуры: якорь `ClientAnchor` (0xF010) с номером
  строки и свойство `pib` (0x0104) со ссылкой на картинку.

**ЗАПИСИ ОБЯЗАТЕЛЬНО СКЛЕИВАТЬ.** Excel режет рисунки на несколько записей подряд, и
структура OfficeArt продолжается через границу: разобрав каждую запись отдельно, мы находим
только то, что поместилось в первую. На боевом файле это и случилось — 8 якорей из 27, и обе
марки оказались как раз среди потерянных (04.10.2026).

**Строка якоря — СЫРАЯ, от единицы**, как и у `parser.image_anchor_rows`: там же, где стоит
левый верхний угол картинки. Пересчёт в нумерацию непустых строк делает `brand_rows`.

Модуль чистый: ни базы, ни сети. Нужен только `xlrd` (он уже в зависимостях — им читается
сам `.xls`), из него берётся разбор OLE2.
"""
from __future__ import annotations

import logging
import re
import struct

logger = logging.getLogger(__name__)

BOF = 0x0809
BOUNDSHEET = 0x0085
MSODRAWINGGROUP = 0x00EB
MSODRAWING = 0x00EC
CONTINUE = 0x003C
OBJ = 0x005D
WORKSHEET = 0x0010

#: OfficeArt: контейнер хранилища картинок, описание картинки, свойства фигуры, якорь.
ART_BSE = 0xF007
ART_OPT = 0xF00B
ART_ANCHOR = 0xF010
ART_BLIP_FIRST, ART_BLIP_LAST = 0xF018, 0xF117
PROP_PIB = 0x0104

#: Как узнать картинку в блипе: перед ней лежат UID и тег переменной длины, и проще найти
#: саму подпись формата, чем считать все варианты заголовка.
SIGNATURES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
              (b"GIF8", "image/gif"), (b"BM", "image/bmp"))
#: Подпись дальше этого места — значит это не заголовок блипа, а совпадение в данных.
SIGNATURE_WINDOW = 80

#: Заголовки OfficeArt, которые ищем сканированием. У якоря на листе длина всегда 18, и этого
#: вместе с типом довольно, чтобы не ловить случайные совпадения; у свойств длина переменная,
#: поэтому читаем и её, и слово версии-инстанса (в нём число свойств).
_ANCHOR_HEAD = re.compile(rb"\x10\xf0\x12\x00\x00\x00")
_OPT_HEAD = re.compile(rb"(..)\x0b\xf0(....)", re.S)


def is_xls(content: bytes) -> bool:
    """Это настоящий OLE2-`.xls`? У `.xlsx` подпись ZIP, у «экселя из HTML» — текст."""
    return content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _workbook_stream(content: bytes) -> bytes:
    from xlrd.compdoc import CompDoc

    doc = CompDoc(content)
    for name in ("Workbook", "Book"):
        found = doc.locate_named_stream(name)
        if found and found[0]:
            return found[0]
    return b""


def _records(stream: bytes):
    """Записи BIFF как есть: [(тип, данные)]. CONTINUE НЕ склеиваем здесь.

    Склейка «присоединить CONTINUE к предыдущей записи» выглядит очевидной и ошибочна: в
    подпотоке листа рисунок идёт вперемешку с записями объектов (`OBJ`), и продолжение
    рисунка нередко следует за `OBJ`, а не за `MSODRAWING`. Приписав такие байты объекту, мы
    теряем их для рисунка — на боевом файле Линдервуда так пропали 19 якорей из 27, и среди
    них обе марки (04.10.2026). Кому принадлежит продолжение, решает `_drawing_bytes`.
    """
    pos, out = 0, []
    while pos + 4 <= len(stream):
        rec, size = struct.unpack("<HH", stream[pos:pos + 4])
        out.append((rec, stream[pos + 4:pos + 4 + size]))
        pos += 4 + size
    return out


def _art(payload: bytes):
    """Записи OfficeArt: (тип, инстанс, данные). В контейнеры заходим."""
    pos = 0
    while pos + 8 <= len(payload):
        verinst, kind, size = struct.unpack("<HHI", payload[pos:pos + 8])
        body = payload[pos + 8:pos + 8 + size]
        yield kind, verinst >> 4, body
        if (verinst & 0x0F) == 0x0F:
            yield from _art(body)
        pos += 8 + size


def _sheet_names(records) -> list[str]:
    names = []
    for rec, payload in records:
        if rec != BOUNDSHEET or len(payload) < 8:
            continue
        length, flags = payload[6], payload[7]
        raw = payload[8:]
        names.append(raw[:length * 2].decode("utf-16-le", "replace") if flags & 1
                     else raw[:length].decode("cp1251", "replace"))
    return names


def _picture(blob: bytes) -> tuple[bytes, str]:
    """Байты картинки и её тип из блипа. Пусто — формат не опознан."""
    best = None
    for signature, media in SIGNATURES:
        at = blob.find(signature)
        if 0 <= at < SIGNATURE_WINDOW and (best is None or at < best[0]):
            best = (at, media)
    if best is None:
        return b"", ""
    at, media = best
    return blob[at:], media


def _drawing_bytes(records, wanted: set[int]) -> dict[int, bytes]:
    """Байты рисунков по листам (−1 — глобальные): продолжения идут ЗА СВОЕЙ записью.

    Так собирается хранилище КАРТИНОК в глобальном подпотоке: там продолжения есть и у
    таблицы строк, и склеив их заодно, мы испортили бы разбор хранилища. У фигур на ЛИСТЕ
    правило другое и нарочно более грубое — см. `_shapes`.
    """
    out: dict[int, bytes] = {}
    sheet = -1
    owner = None
    for rec, payload in records:
        if rec == BOF:
            kind = struct.unpack("<H", payload[2:4])[0] if len(payload) >= 4 else 0
            if kind == WORKSHEET:
                sheet += 1
            owner = None
            continue
        if rec in wanted:
            owner = sheet
            out[sheet] = out.get(sheet, b"") + payload
        elif rec == CONTINUE and owner is not None:
            out[owner] = out.get(owner, b"") + payload
        elif rec != OBJ:
            # Любая другая запись закрывает рисунок: её продолжения — не наши.
            owner = None
    return out


def _blips(records) -> list[tuple[bytes, str]]:
    """Картинки книги по порядку: ссылка `pib` — это номер в этом списке, от единицы."""
    glued = b"".join(_drawing_bytes(records, {MSODRAWINGGROUP}).values())
    out: list[tuple[bytes, str]] = []
    for kind, _inst, body in _art(glued):
        if kind != ART_BSE or len(body) < 36:
            continue
        # Заголовок BSE: 36 байт, затем необязательное имя длиной `cbName`, затем блип.
        rest = body[36 + body[33]:]
        blip = next((data for sub, _i, data in _art(rest)
                     if ART_BLIP_FIRST <= sub <= ART_BLIP_LAST), rest)
        out.append(_picture(blip))
    return out


def _shapes(records) -> dict[int, list[tuple[int, int]]]:
    """Фигуры по листам: {номер листа: [(строка, номер картинки)]}.

    **БЕРЁМ ВСЕ ПРОДОЛЖЕНИЯ ПОДПОТОКА, не разбирая, чьи они.** Рисунок на листе перемежается
    записями объектов (`OBJ`) и текстовых объектов (`TXO`), и его продолжения идут то за
    одними, то за другими: на боевом файле Линдервуда 9 якорей лежали за `OBJ` и ещё 10 — за
    `TXO`, то есть ровно обе марки. Угадывать владельца незачем — ниже стоит СКАНИРОВАНИЕ по
    заголовкам, и лишние байты ему не мешают, а вот недостающие стоили бы потерянных брендов.
    """
    by_sheet: dict[int, bytes] = {}
    sheet = -1
    for rec, payload in records:
        if rec == BOF:
            kind = struct.unpack("<H", payload[2:4])[0] if len(payload) >= 4 else 0
            if kind == WORKSHEET:
                sheet += 1
            continue
        if sheet >= 0 and rec in (MSODRAWING, CONTINUE):
            by_sheet[sheet] = by_sheet.get(sheet, b"") + payload

    found = {sheet: _scan_shapes(glued) for sheet, glued in by_sheet.items()}
    return {sheet: shapes for sheet, shapes in found.items() if shapes}


def _scan_shapes(glued: bytes) -> list[tuple[int, int]]:
    """Якоря и ссылки на картинки — СКАНИРОВАНИЕМ по заголовкам, а не обходом вложенности.

    **ПОЧЕМУ НЕ ОБХОДОМ.** Последовательный разбор контейнеров держится на том, что все
    размеры сходятся; одна запись с неожиданной длиной — и остаток буфера пропущен молча. На
    боевом файле Линдервуда так и вышло: обход находил 5 фигур из 27, и обе марки (Peli на
    строке 5, LINDERWOOD на 63) оказывались среди потерянных.

    Порядок записей внутри фигуры задан форматом: свойства (`OPT`) идут ПЕРЕД якорем, поэтому
    картинкой якоря считается последняя ссылка `pib`, встреченная выше по байтам.
    """
    marks: list[tuple[int, int, object]] = []
    for match in _ANCHOR_HEAD.finditer(glued):
        marks.append((match.end(), 1, glued[match.end():match.end() + 18]))
    for match in _OPT_HEAD.finditer(glued):
        verinst, size = struct.unpack("<HI", match.group(1) + match.group(2))
        if 0 < size <= len(glued):
            marks.append((match.end(), 0, (verinst >> 4,
                                           glued[match.end():match.end() + size])))

    out: list[tuple[int, int]] = []
    pib = 0
    for _at, kind, body in sorted(marks, key=lambda m: (m[0], m[1])):
        if kind == 0:
            count, data = body
            for index in range(min(count, len(data) // 6)):
                prop, value = struct.unpack("<HI", data[index * 6:index * 6 + 6])
                if (prop & 0x3FFF) == PROP_PIB:
                    pib = value
        elif len(body) >= 18:
            fields = struct.unpack("<9H", body[:18])
            out.append((fields[3] + 1, pib))
            pib = 0
    return out


def xls_images(content: bytes) -> dict[str, list[tuple[int, bytes, str]]]:
    """Картинки `.xls` по листам: {лист: [(строка, байты, media_type)]}.

    Не `.xls`, нет картинок, сломанный файл — пустой словарь: отсутствие баннеров это самый
    обычный прайс, а не ошибка.
    """
    if not is_xls(content):
        return {}
    try:
        stream = _workbook_stream(content)
        if not stream:
            return {}
        records = _records(stream)
        names = _sheet_names(records)
        blips = _blips(records)
        shapes = _shapes(records)
    except Exception:                                   # noqa: BLE001
        logger.warning("Картинки .xls не прочитались", exc_info=True)
        return {}

    out: dict[str, list[tuple[int, bytes, str]]] = {}
    for sheet, found in shapes.items():
        name = names[sheet] if sheet < len(names) else f"Лист{sheet + 1}"
        for row, pib in found:
            if not 0 < pib <= len(blips):
                continue
            data, media = blips[pib - 1]
            if data:
                out.setdefault(name, []).append((row, data, media))
    return out


def xls_anchor_rows(content: bytes) -> dict[str, list[int]]:
    """Только строки-якоря: {лист: [строки]} — как `parser.image_anchor_rows` у `.xlsx`."""
    return {name: sorted(row for row, _data, _media in found)
            for name, found in xls_images(content).items()}
