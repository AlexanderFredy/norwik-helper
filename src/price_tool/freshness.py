"""Какой прайс свежее (§9.8).

Вопрос возникает дважды: чем заменить версию, стоящую в очереди, и не устарела ли
отложенная задача. Ответ нужен ДО того, как модель посмотрела файл, — очередь наполняется
без её участия, а в будущем и вовсе из почты.

ТРИ ИСТОЧНИКА ДАТЫ, ПО УБЫВАНИЮ ТОЧНОСТИ
----------------------------------------
1. **Дата из самого прайса** — шапка листа или имя файла. Самая точная: её ставит
   поставщик, и именно она отличает «прайс с 20.07» от «прайса с 25.08». Но бывает, что её
   нет вовсе, а бывает, что поставщик забыл поменять шапку при переэкспорте.
2. **Дата письма**, которым прайс пришёл. Не говорит, за какое число прайс, зато надёжно
   отвечает на другой вопрос: что пришло позже. Письмо не может прийти раньше, чем прайс
   составлен.
3. **Момент получения файла ботом** — когда ни того, ни другого нет.

ПОЧЕМУ ОДНОЙ ДАТЫ ИЗ ПРАЙСА МАЛО. Её может не быть (у Most Floor в шапке только «Прайс
лист»), она может быть не распознана, и она может быть старее у более нового файла —
переэкспорт со старой шапкой обычное дело. Поэтому даты сравниваются по очереди: сначала
даты прайсов, и только если они равны или неизвестны — даты получения.

ДАТА ИЗ ИМЕНИ ФАЙЛА СЧИТАЕТСЯ КОДОМ, а не моделью: очередь наполняется раньше, чем модель
увидит файл, а «Прайс Монарх с 20.07.xlsx» разбирается однозначно.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

# Дата в имени файла. Порядок важен: сначала формы с годом, иначе «01.09» съест начало
# «01.09.2026». Двузначный год считаем двадцать первым веком — прайсов из 1999 года нет.
_PATTERNS = (
    re.compile(r"(?<!\d)(\d{4})[-._](\d{1,2})[-._](\d{1,2})(?!\d)"),          # 2026-09-01
    re.compile(r"(?<!\d)(\d{1,2})[-._](\d{1,2})[-._](\d{4})(?!\d)"),          # 01.09.2026
    re.compile(r"(?<!\d)(\d{1,2})[-._](\d{1,2})[-._](\d{2})(?!\d)"),          # 01.09.26
    re.compile(r"(?<!\d)(\d{1,2})[-._](\d{1,2})(?!\d)"),                      # 01.09
)


def _try_date(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def date_from_name(filename: str | None) -> str | None:
    """`ГГГГ-ММ-ДД` из имени файла. None — даты не видно.

    Дата без года относится к текущему году; если так выходит будущее дальше, чем на месяц,
    берём прошлый год: «Прайс с 20.12», присланный в январе, — это декабрь, а не декабрь
    впереди.
    """
    text = str(filename or "")

    for pattern in _PATTERNS[:2]:
        m = pattern.search(text)
        if m:
            a, b, c = (int(x) for x in m.groups())
            found = _try_date(a, b, c) if a > 31 else _try_date(c, b, a)
            if found:
                return found

    m = _PATTERNS[2].search(text)
    if m:
        day, month, year = (int(x) for x in m.groups())
        found = _try_date(2000 + year, month, day)
        if found:
            return found

    m = _PATTERNS[3].search(text)
    if m:
        day, month = (int(x) for x in m.groups())
        today = date.today()
        found = _try_date(today.year, month, day)
        if found and (date.fromisoformat(found) - today).days > 31:
            found = _try_date(today.year - 1, month, day)
        return found

    return None


_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7,
           "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
_WORDY = re.compile(r"(?<!\d)(\d{1,2})\s+([а-яё]+)\s+(\d{4})", re.I)
#: Сколько первых строк листа считать шапкой. Ниже идут товары, и дата в их ячейках —
#: срок поставки или дата поступления, а не дата прайса.
HEAD_ROWS = 12


def date_from_sheets(sheets) -> str | None:
    """`ГГГГ-ММ-ДД` из шапки листа: «Прайс-лист на 26.08.2026», «на 1 октября 2026 г».

    Только даты С ГОДОМ: короткое «01.09» в шапке бывает чем угодно — от номера раздела до
    размера. Берём первую найденную по порядку листов и строк: дату прайса поставщик ставит
    наверх (бой 08.10.2026: у Артисаны она в пятой строке, а имя файла — просто «Price.xls»,
    и прайс жил без даты, отчего его цены не старели в выборе наименьшей никогда).
    """
    for sheet in sheets or []:
        for row in (getattr(sheet, "rows", None) or [])[:HEAD_ROWS]:
            for cell in row:
                text = str(cell or "")
                if not re.search(r"\d{4}", text):
                    continue
                for pattern in _PATTERNS[:2]:
                    m = pattern.search(text)
                    if m:
                        a, b, c = (int(x) for x in m.groups())
                        found = _try_date(a, b, c) if a > 31 else _try_date(c, b, a)
                        if found:
                            return found
                m = _WORDY.search(text)
                if m:
                    word = m.group(2).lower()
                    month = next((n for stem, n in _MONTHS.items()
                                  if word.startswith(stem) and (stem != "ма" or word in
                                                                ("мая", "май"))), None)
                    if month:
                        found = _try_date(int(m.group(3)), month, int(m.group(1)))
                        if found:
                            return found
    return None


def now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _day(value: str | None) -> str:
    return (str(value or "")[:10]) if len(str(value or "")) >= 10 else ""


def is_newer(incoming: dict, existing: dict) -> bool:
    """Свежее ли `incoming`. Сравнение по очереди: дата прайса, потом дата получения.

    Неизвестные даты ничего не решают: при полном незнании побеждает то, что пришло
    позже, — иначе повторная присылка того же файла ничего бы не обновляла, а именно ею
    админ и возвращается к прайсу.
    """
    new_price, old_price = _day(incoming.get("price_date")), _day(existing.get("price_date"))
    if new_price and old_price and new_price != old_price:
        return new_price > old_price

    new_seen = str(incoming.get("received_at") or "")
    old_seen = str(existing.get("received_at") or "")
    if new_seen and old_seen and new_seen != old_seen:
        return new_seen > old_seen

    return True


def human_date(row: dict) -> str:
    """«11.09.26» — дата прайса, а если её нет, дата получения. Пусто, если нет ничего."""
    stamp = _day(row.get("price_date")) or _day(row.get("received_at"))
    if not stamp:
        return ""
    try:
        return date.fromisoformat(stamp).strftime("%d.%m.%y")
    except ValueError:
        return ""
