"""Определения инструментов для Claude и их выполнение.

Кастомные инструменты выполняются на нашей стороне (email, norwik.ru);
web_search — серверный инструмент Anthropic (для случая А из specs/master-spec.md).
"""
import asyncio
import json
import logging
import re
from datetime import date

from src.email_tool.attachments import excel_sheet_names, extract_text
from src.email_tool.classifier import classify, parse_signature
from src.email_tool.client import MailClient
from src.price_tool.history import describe_group, describe_product
from src.website_tool.norwik import NorwikClient

logger = logging.getLogger(__name__)


def _tokens(text: str | None) -> set[str]:
    return set(re.findall(r"[0-9a-zа-яё]+", (text or "").lower()))


def _match_products(items: list, query: str) -> list:
    """Товары под запрос менеджера: сначала точный артикул, иначе все слова в названии."""
    exact = [i for i in items if i.article and i.article.lower() == query.lower().strip()]
    if exact:
        return exact
    wanted = _tokens(query)
    if not wanted:
        return []
    return [i for i in items if wanted <= _tokens(i.name) | _tokens(i.article)]

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
    {
        "name": "get_price_history",
        "description": (
            "Когда в 1С последний раз меняли цены и из какого прайса они взяты. "
            "Отвечает на вопросы вида «когда меняли цены на Classen Adventure?». "
            "Обязателен tm — название торговой марки; уточни collection (коллекция) или "
            "product (конкретный товар, название или артикул), если менеджер их назвал. "
            "Возвращает ГОТОВЫЙ текст ответа — передай его менеджеру как есть, ничего не "
            "пересчитывая и не додумывая источник цены."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tm": {"type": "string", "description": "торговая марка, напр. Classen"},
                "collection": {"type": "string", "description": "коллекция, напр. Adventure"},
                "product": {"type": "string", "description": "название или артикул товара"},
            },
            "required": ["tm"],
            "additionalProperties": False,
        },
    },
    # Серверный инструмент Anthropic — поиск сайта производителя (случай А)
    {"type": "web_search_20260209", "name": "web_search"},
]


class ToolExecutor:
    """Выполняет кастомные инструменты. Серверные (web_search) выполняет API."""

    def __init__(self, mail: MailClient, norwik: NorwikClient, onec=None,
                 pricing_store=None) -> None:
        self._mail = mail
        self._norwik = norwik
        self._onec = onec              # None, если интеграция с 1С не настроена
        self._pricing_store = pricing_store

    async def execute(self, name: str, tool_input: dict) -> str:
        try:
            if name == "get_price_history":
                return await self._price_history(tool_input)
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

    # ------------------------------------------------- история цен (dev_tasks п.6)

    async def _sources(self, items: list) -> dict:
        """Журнал наших записей для тех дат, которые показывает 1С."""
        if self._pricing_store is None:
            return {}
        refs, dates = [], []
        for item in items:
            for kind in ("purchase", "retail", "rrc"):
                price = getattr(item, kind, None)
                if price and price.date:
                    refs.append(item.ref)
                    dates.append(price.date)
        return await self._pricing_store.price_sources(refs, dates)

    async def _price_history(self, inp: dict) -> str:
        if self._onec is None:
            return "История цен недоступна: интеграция с 1С не настроена."

        tms = await asyncio.to_thread(self._onec.selling_tm)
        wanted = (inp.get("tm") or "").strip().lower()
        tm = next((t for t in tms if wanted and wanted in t.name.lower()), None)
        if tm is None:
            names = ", ".join(t.name for t in tms) or "список пуст"
            return f"ТМ «{inp.get('tm')}» нет в выгрузке на сайт. Есть: {names}"

        nom = await asyncio.to_thread(self._onec.by_tm_all, tm.code)
        items = nom.items

        def finish(text: str) -> str:
            """1С могла не отдать часть позиций — тогда ответ неполный, и это надо сказать."""
            if not nom.errors:
                return text
            return (f"{text}\n\n(1С не отдала {len(nom.errors)} поз. по этой марке — "
                    "по ним ответить не могу.)")

        if not items:
            return f"У ТМ {tm.name} нет товаров в выгрузке."

        collection = (inp.get("collection") or "").strip()
        if collection:
            items = [i for i in items
                     if collection.lower() in (i.collection or i.parent or "").lower()]
            if not items:
                return finish(f"Коллекция «{collection}» у ТМ {tm.name} не найдена.")

        sources = await self._sources(items)
        product = (inp.get("product") or "").strip()
        if product:
            found = _match_products(items, product)
            if not found:
                return finish(f"Товар «{product}» не найден у ТМ {tm.name}.")
            if len(found) == 1:
                return finish(describe_product(found[0], sources))
            if len(found) <= 5:
                return finish("Подходит несколько товаров:\n" + "\n".join(
                    describe_product(i, sources) for i in found))
            return finish(describe_group(f"«{product}» у {tm.name}", found, sources))

        if collection:
            title = items[0].collection or items[0].parent or collection
            return finish(describe_group(f"{tm.name} {title}", items, sources))

        # запрос по ТМ целиком — разбираем по коллекциям в той же логике (п.6 ТЗ)
        groups: dict[str, list] = {}
        for item in items:
            groups.setdefault(item.collection_ref or item.collection or "", []).append(item)
        lines = [f"{tm.name}: {len(items)} товаров в {len(groups)} коллекциях."]
        ordered = sorted(groups.values(), key=lambda g: (g[0].collection or g[0].parent or ""))
        for group in ordered[:40]:
            title = group[0].collection or group[0].parent or "без коллекции"
            lines.append("• " + describe_group(title, group, sources))
        if len(ordered) > 40:
            lines.append(f"... и ещё {len(ordered) - 40} коллекций — уточни, какая нужна.")
        return finish("\n".join(lines))

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
