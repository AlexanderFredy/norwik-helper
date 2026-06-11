"""Классификация письма (прайс-лист / остатки) и парсинг подписи.

Порядок проверки типа (spec.md, раздел 4):
1. тема письма → 2. имена вложений → 3. названия листов Excel.
"""
import re
from dataclasses import dataclass
from enum import Enum

PRICE_KEYWORDS = ("прайс", "price", "цены", "цена")
STOCK_KEYWORDS = ("остатки", "остаток", "склад", "наличие", "stock")


class MailKind(Enum):
    PRICE = "price"
    STOCK = "stock"
    UNKNOWN = "unknown"


def _kind_from_text(text: str) -> MailKind:
    lowered = text.lower()
    has_price = any(k in lowered for k in PRICE_KEYWORDS)
    has_stock = any(k in lowered for k in STOCK_KEYWORDS)
    if has_price and not has_stock:
        return MailKind.PRICE
    if has_stock and not has_price:
        return MailKind.STOCK
    return MailKind.UNKNOWN


def classify(
    subject: str,
    attachment_names: list[str],
    excel_sheet_names: list[str] | None = None,
) -> MailKind:
    """Определяет тип письма по приоритету: тема → вложения → листы Excel."""
    for source in (
        subject,
        " ".join(attachment_names),
        " ".join(excel_sheet_names or []),
    ):
        kind = _kind_from_text(source)
        if kind is not MailKind.UNKNOWN:
            return kind
    return MailKind.UNKNOWN


@dataclass(frozen=True)
class SupplierContact:
    name: str | None
    phone: str | None


_SIGNATURE_MARKERS = re.compile(
    r"(?:^--\s*$|с уважением|best regards|регион[ао]льный менеджер|менеджер)",
    re.IGNORECASE | re.MULTILINE,
)
_PHONE_RE = re.compile(
    r"(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}"
)
def parse_signature(body_text: str) -> SupplierContact:
    """Извлекает имя менеджера и телефон из подписи письма.

    Подпись ищется после маркера («--», «С уважением», должность);
    если маркера нет — анализируются последние 10 строк письма.
    """
    match = _SIGNATURE_MARKERS.search(body_text)
    if match:
        block = body_text[match.start():]
    else:
        lines = body_text.rstrip().splitlines()
        block = "\n".join(lines[-10:])

    phone_match = _PHONE_RE.search(block)
    phone = phone_match.group(0).strip() if phone_match else None

    name = None
    sig_match = re.search(
        r"(?i:с уважением)[,!.\s]*\n\s*([А-ЯЁ][а-яё]+(?:[ \t]+[А-ЯЁ][а-яё]+){0,2})",
        block,
    )
    if sig_match:
        name = sig_match.group(1).strip()
    else:
        for line in block.splitlines():
            line = line.strip().rstrip(",")
            if re.fullmatch(r"[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,2}", line):
                name = line
                break

    return SupplierContact(name=name, phone=phone)
