"""Сигнатура структуры прайса — ключ запоминаемого маппинга колонок (§6.5.2 спеки).

Считается по «скелету» файла: имена листов + верхние строки с заголовками, БЕЗ данных.
Тот же формат прайса → та же сигнатура → маппинг подхватывается без вопросов админу.

Числа заменяются на «#»: иначе «Прайс с 20.07» и «Прайс с 25.08» дали бы разные сигнатуры,
и маппинг терялся бы каждый месяц — ровно то, ради чего он и заводится.
"""
from __future__ import annotations

import hashlib
import json
import re

from src.price_tool.parser import Sheet

HEAD_ROWS = 8      # сколько текстовых строк берём за «шапку»
HEAD_CELLS = 25    # и сколько ячеек в строке
SCAN_ROWS = 40     # как глубоко ищем шапку, прежде чем сдаться

_DIGITS = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")
_NUMERIC = re.compile(r"^[#\s.,%+\-/×x*()]+$")   # ячейка без букв — это данные, не заголовок


def _norm(cell: str) -> str:
    return _SPACES.sub(" ", _DIGITS.sub("#", cell).strip().lower())


def _is_data_row(cells: list[str]) -> bool:
    """Строка с числом (цена, артикул, размер) — данные: они меняются от прайса к прайсу."""
    return any(_NUMERIC.fullmatch(c) for c in cells)


def _skeleton(sheets: list[Sheet]) -> list[dict]:
    out = []
    for sheet in sheets:
        head: list[list[str]] = []
        for row in sheet.rows[:SCAN_ROWS]:
            cells = [_norm(c) for c in row[:HEAD_CELLS] if c and c.strip()]
            if not cells or _is_data_row(cells):
                continue
            head.append(cells)
            if len(head) >= HEAD_ROWS:
                break
        out.append({"sheet": _norm(sheet.name), "head": head})
    return out


def price_signature(sheets: list[Sheet], text: str | None = None) -> str:
    """Устойчивый ключ формата прайса.

    `text` — фолбэк для файлов без таблиц (pdf): скелет строится по верхним строкам
    извлечённого текста. Без него у pdf-прайсов маппинг не запоминался бы вовсе, а
    неоднозначные колонки как раз и встретились в pdf.
    """
    if sheets:
        raw = json.dumps(_skeleton(sheets), ensure_ascii=False)
    elif text and text.strip():
        head = []
        for line in text.splitlines()[:SCAN_ROWS * 3]:
            cells = [_norm(c) for c in line.split("\t") if c and c.strip()] or \
                    ([_norm(line)] if line.strip() else [])
            if not cells or _is_data_row(cells):
                continue
            head.append(cells[:HEAD_CELLS])
            if len(head) >= HEAD_ROWS * 2:      # у pdf строки короче — берём больше
                break
        raw = json.dumps({"text_head": head}, ensure_ascii=False)
    else:
        return ""
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
