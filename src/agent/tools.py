"""Определения инструментов для Claude и их выполнение.

Кастомные инструменты выполняются на нашей стороне (email, norwik.ru);
web_search — серверный инструмент Anthropic (для случая А из spec.md).
"""
import asyncio
import json
import logging
from datetime import date

from src.email_tool.attachments import excel_sheet_names, extract_text
from src.email_tool.classifier import classify, parse_signature
from src.email_tool.client import MailClient
from src.website_tool.norwik import NorwikClient

logger = logging.getLogger(__name__)

TOOL_DEFINITIONS = [
    {
        "name": "search_emails",
        "description": (
            "Поиск писем в почтовом ящике (от поставщиков). Возвращает список писем "
            "от новых к старым: uid, отправитель, тема, дата, текст письма, имена вложений. "
            "Вызывай с параметром text для поиска товара/бренда по содержимому, "
            "либо с sender для всех писем конкретного поставщика."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sender": {"type": "string", "description": "Email отправителя (фильтр FROM)"},
                "subject": {"type": "string", "description": "Подстрока в теме письма"},
                "text": {"type": "string", "description": "Подстрока в теле письма (поиск товара/бренда)"},
                "since": {"type": "string", "description": "Дата ГГГГ-ММ-ДД — только письма новее"},
                "limit": {"type": "integer", "description": "Максимум писем (по умолчанию 20)"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_attachment",
        "description": (
            "Читает вложение письма и возвращает его текстовое содержимое "
            "(таблицы — строками с табуляцией). Поддерживает xlsx, docx, pdf, csv, txt. "
            "Используй для чтения прайс-листов и остатков."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {"type": "string", "description": "UID письма из search_emails"},
                "filename": {"type": "string", "description": "Имя вложения"},
            },
            "required": ["uid", "filename"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_email_contacts",
        "description": (
            "Извлекает контакты поставщика из письма: имя менеджера и телефон из подписи, "
            "email отправителя. Также возвращает классификацию письма (прайс/остатки)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "uid": {"type": "string", "description": "UID письма из search_emails"},
            },
            "required": ["uid"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_norwik",
        "description": (
            "Поиск товара на сайте norwik.ru по названию. "
            "Возвращает список: ID товара, название, ссылка."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Название товара для поиска"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_norwik_product",
        "description": (
            "Карточка товара на norwik.ru по ID: название, текущая цена, ссылка. "
            "Используй, когда известен ID товара."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "integer", "description": "ID товара на сайте"},
            },
            "required": ["product_id"],
            "additionalProperties": False,
        },
    },
    # Серверный инструмент Anthropic — поиск сайта производителя (случай А)
    {"type": "web_search_20260209", "name": "web_search"},
]


class ToolExecutor:
    """Выполняет кастомные инструменты. Серверные (web_search) выполняет API."""

    def __init__(self, mail: MailClient, norwik: NorwikClient) -> None:
        self._mail = mail
        self._norwik = norwik

    async def execute(self, name: str, tool_input: dict) -> str:
        try:
            if name == "search_emails":
                return await self._search_emails(tool_input)
            if name == "read_attachment":
                return await self._read_attachment(tool_input)
            if name == "get_email_contacts":
                return await self._get_email_contacts(tool_input)
            if name == "search_norwik":
                return await self._search_norwik(tool_input)
            if name == "get_norwik_product":
                return await self._get_norwik_product(tool_input)
            return f"Неизвестный инструмент: {name}"
        except Exception as exc:
            logger.exception("Ошибка инструмента %s", name)
            return f"Ошибка выполнения {name}: {exc}"

    async def _search_emails(self, inp: dict) -> str:
        since = date.fromisoformat(inp["since"]) if inp.get("since") else None
        messages = await asyncio.to_thread(
            self._mail.search,
            sender=inp.get("sender"),
            subject=inp.get("subject"),
            text=inp.get("text"),
            since=since,
            limit=inp.get("limit", 20),
        )
        result = [
            {
                "uid": m.uid,
                "from": f"{m.sender_name} <{m.sender_email}>",
                "subject": m.subject,
                "date": m.date.strftime("%Y-%m-%d"),
                "body_preview": m.body_text[:500],
                "attachments": [],  # имена вложений доступны через get_email_contacts/read_attachment
            }
            for m in messages
        ]
        # имена вложений без скачивания контента дорого получить через IMAP —
        # отдаём их при полном чтении письма
        return json.dumps(result, ensure_ascii=False)

    async def _full_message(self, uid: str):
        return await asyncio.to_thread(self._mail.fetch_message, uid)

    async def _read_attachment(self, inp: dict) -> str:
        msg = await self._full_message(inp["uid"])
        for att in msg.attachments:
            if att.filename == inp["filename"]:
                text = extract_text(att.filename, att.content)
                limit = 30000
                if len(text) > limit:
                    text = text[:limit] + f"\n... (обрезано, всего {len(text)} символов)"
                return text
        names = [a.filename for a in msg.attachments]
        return f"Вложение не найдено. Доступные вложения: {names}"

    async def _get_email_contacts(self, inp: dict) -> str:
        msg = await self._full_message(inp["uid"])
        contact = parse_signature(msg.body_text)
        sheet_names: list[str] = []
        for att in msg.attachments:
            if att.filename.lower().endswith(".xlsx"):
                try:
                    sheet_names += excel_sheet_names(att.content)
                except Exception:
                    pass
        kind = classify(msg.subject, [a.filename for a in msg.attachments], sheet_names)
        return json.dumps(
            {
                "sender_name": msg.sender_name,
                "sender_email": msg.sender_email,
                "manager_name": contact.name,
                "phone": contact.phone,
                "mail_kind": kind.value,
                "attachments": [a.filename for a in msg.attachments],
                "date": msg.date.strftime("%Y-%m-%d"),
            },
            ensure_ascii=False,
        )

    async def _search_norwik(self, inp: dict) -> str:
        results = await asyncio.to_thread(self._norwik.search, inp["query"])
        return json.dumps(
            [{"id": r.product_id, "title": r.title, "url": r.url} for r in results],
            ensure_ascii=False,
        )

    async def _get_norwik_product(self, inp: dict) -> str:
        product = await asyncio.to_thread(self._norwik.get_product, inp["product_id"])
        if product is None:
            return "Товар не найден на norwik.ru"
        return json.dumps(
            {
                "id": product.product_id,
                "title": product.title,
                "price": product.price,
                "currency": product.currency,
                "url": product.url,
            },
            ensure_ascii=False,
        )
