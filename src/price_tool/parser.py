"""Структурный разбор файла прайса в строки (specs/content-manager.md §6.7).

В отличие от email_tool.attachments.extract_text (текст с обрезкой 30k для LLM),
здесь возвращаем полные строки таблицы для сигнатуры/маппинга/сопоставления.
Форматы: .xlsx (openpyxl, data_only), .xls (xlrd), .csv. Ошибки не бросаются.
"""
import csv
import io
import logging
from dataclasses import dataclass, field
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)


@dataclass
class Sheet:
    name: str
    rows: list[list[str]] = field(default_factory=list)


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _from_xlsx(content: bytes) -> list[Sheet]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    sheets: list[Sheet] = []
    try:
        for ws in wb.worksheets:
            rows = [
                [_cell(c) for c in row]
                for row in ws.iter_rows(values_only=True)
            ]
            sheets.append(Sheet(name=ws.title, rows=rows))
    finally:
        wb.close()
    return sheets


def _from_xls(content: bytes) -> list[Sheet]:
    import xlrd

    wb = xlrd.open_workbook(file_contents=content)
    sheets: list[Sheet] = []
    for sh in wb.sheets():
        rows = [
            [_cell(sh.cell_value(i, j)) for j in range(sh.ncols)]
            for i in range(sh.nrows)
        ]
        sheets.append(Sheet(name=sh.name, rows=rows))
    return sheets


def _from_csv(content: bytes) -> list[Sheet]:
    for enc in ("utf-8-sig", "cp1251", "utf-8"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = content.decode("utf-8", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), dialect)]
    return [Sheet(name="csv", rows=rows)]


def parse_price_table(content: bytes, filename: str) -> list[Sheet]:
    """Разбирает прайс в список листов со строками. Пустой список — если не разобрать."""
    ext = PurePosixPath(filename.lower()).suffix
    try:
        if ext == ".xlsx":
            return _from_xlsx(content)
        if ext == ".xls":
            return _from_xls(content)
        if ext == ".csv":
            return _from_csv(content)
    except Exception:
        logger.exception("Не удалось разобрать прайс %s", filename)
        return []
    logger.warning("Формат %s не поддержан парсером прайса", ext)
    return []


def _rtrim(row: list[str]) -> list[str]:
    """Убирает хвостовые пустые ячейки (листы бывают «широкими» — сотни пустых колонок)."""
    i = len(row)
    while i > 0 and not row[i - 1]:
        i -= 1
    return row[:i]


def non_empty_rows(sheet: Sheet) -> list[list[str]]:
    return [_rtrim(r) for r in sheet.rows if any(c for c in r)]


def render_preview(sheet: Sheet, max_rows: int = 250) -> str:
    """Таб-текст листа для передачи агенту (без хвостовых пустых ячеек, лимит строк)."""
    rows = non_empty_rows(sheet)
    lines = [f"=== Лист: {sheet.name} ({len(rows)} непустых строк) ==="]
    for r in rows[:max_rows]:
        lines.append("\t".join(r))
    if len(rows) > max_rows:
        lines.append(f"... (показаны первые {max_rows} из {len(rows)} строк)")
    return "\n".join(lines)
