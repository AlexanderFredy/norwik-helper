"""Определения инструментов для Claude и их выполнение.

Кастомные инструменты выполняются на нашей стороне (email, norwik.ru);
web_search — серверный инструмент Anthropic (для случая А из specs/master-spec.md).
"""
import asyncio
import json
import logging
import re
from datetime import date
from pathlib import Path

from src.email_tool.attachments import excel_sheet_names, extract_text
from src.email_tool.classifier import classify, parse_signature
from src.email_tool.client import MailClient
from src.model import normalize as nz
from src.price_tool.exclusive import find, resolve
from src.price_tool.history import describe_group, describe_product
from src.website_tool import photo_report, photos
from src.website_tool.norwik import NorwikClient

logger = logging.getLogger(__name__)


def _tokens(text: str | None) -> set[str]:
    return set(re.findall(r"[0-9a-zа-яё]+", (text or "").lower()))


def _months_ago(months: int, today: date | None = None) -> str:
    """Дата «столько-то месяцев назад», ГГГГ-ММ-ДД.

    Считаем ПО КАЛЕНДАРЮ, а не «месяц = 30 дней»: три месяца от 30 ноября это 28 февраля,
    и разница с арифметикой по дням доходит до трёх суток — как раз столько, сколько
    товаров заводят за день. Число месяца, которого в целевом месяце нет (31 мая → 31
    февраля), сдвигаем на первое следующего: граница «с какого дня считать новым» должна
    существовать в календаре.

    Время в чистой функции передаётся явно — иначе тест пришлось бы привязывать к
    сегодняшнему дню.
    """
    today = today or date.today()
    month = today.month - months
    year = today.year
    while month <= 0:
        month += 12
        year -= 1
    try:
        return date(year, month, today.day).isoformat()
    except ValueError:
        return (date(year + month // 12, month % 12 + 1, 1)).isoformat()


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
    {
        "name": "find_items_without_photo",
        "description": (
            "Новые товары, у которых на сайте нет ни одного фото. Отвечает на вопросы "
            "вида «покажи, где не добавлены фото», «у каких новых товаров нет "
            "фотографий». Смотрит только товары, заведённые за последние месяцы "
            "(months, по умолчанию 3) и выгружаемые на сайт; tm сужает до одной марки, "
            "если менеджер её назвал. "
            "СПИСОК УЖЕ ОТПРАВЛЕН менеджеру — отдельным сообщением или файлом Excel, "
            "смотря по длине. Тебе возвращаются ТОЛЬКО ЧИСЛА: ответь по ним одной "
            "фразой и не пересказывай список, не придумывай наименования и ссылки."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tm": {"type": "string",
                       "description": "торговая марка, если менеджер её назвал"},
                "months": {"type": "integer",
                           "description": "за сколько последних месяцев, по умолчанию 3"},
            },
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
        # ГОТОВЫЙ ОТВЕТ МИМО МОДЕЛИ. Длинный список (товары без фото) она обязана была бы
        # пересказать целиком — это выходные токены за копирование и риск, что ссылки в
        # пересказе разойдутся с настоящими. Инструмент кладёт список сюда, обработчик
        # отправляет его сам, модель получает только числа. Тот же приём, что `last_summary`
        # в прайсовом потоке.
        self.pending_message: str | None = None
        self.pending_file = None                # Path | None — Excel, когда в чат не влез

    def take_pending(self) -> tuple[str | None, object | None]:
        """Забрать отложенное и ОЧИСТИТЬ. Очистка обязательна: оставшийся список уехал бы
        второй раз в ответ на следующий, совсем другой вопрос."""
        message, file = self.pending_message, self.pending_file
        self.pending_message, self.pending_file = None, None
        return message, file

    async def execute(self, name: str, tool_input: dict) -> str:
        try:
            if name == "get_price_history":
                return await self._price_history(tool_input)
            if name == "find_items_without_photo":
                return await self._photos_missing(tool_input)
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

    async def _exclusives(self) -> dict:
        """Действующие пометки об эксклюзиве (§9.5) — справочно, к ценам отношения не имеют."""
        if self._pricing_store is None:
            return {}
        active, _ = resolve(*await self._pricing_store.load_exclusives())
        return active

    # ------------------------------------------------ фото на сайте (решение 30.09.2026)

    async def _photos_missing(self, inp: dict) -> str:
        """Новые товары без фото на сайте.

        ТОТАЛЬНОГО ОБХОДА КАТАЛОГА ЗДЕСЬ НЕТ — это решение админа, а не упрощение: вопрос
        про недавно заведённые товары, а «все товары без фото» будет отдельным
        инструментом. Отбор идёт по `ДатаСоздания` на стороне 1С, иначе пришлось бы тянуть
        каталог каждой марки целиком, чтобы отбросить его почти весь.
        """
        if self._onec is None:
            return "Проверка фото недоступна: интеграция с 1С не настроена."

        months = max(1, min(12, int(inp.get("months") or 3)))
        since = _months_ago(months)
        wanted = (inp.get("tm") or "").strip()

        # ТОЛЬКО ПОМЕЧЕННЫЕ К ВЫГРУЗКЕ марки: товар непомеченной на сайт не попадает, и
        # спрашивать о его фото бессмысленно. Второе условие — сам товар не в папке
        # «Не выгружать» — держит `by_tm_all` своим умолчанием.
        marks = await asyncio.to_thread(self._onec.selling_tm)
        if wanted:
            marks = [m for m in marks if wanted.lower() in m.name.lower()]
            if not marks:
                return f"Марки «{wanted}» нет среди выгружаемых на сайт."

        fresh, lost, dated = [], 0, False
        for mark in marks:
            nom = await asyncio.to_thread(self._onec.by_tm_all, mark.code,
                                          created_from=since)
            lost += len(nom.errors)
            dated = dated or any(i.created for i in nom.items)
            for item in nom.items:
                # Отбор ПОВТОРЯЕТСЯ здесь, а не доверяется 1С: со старым by-tm.bsl параметр
                # `created_from` проходит мимо, и выгрузка приходит целиком.
                if not item.not_exported and item.created and item.created >= since:
                    fresh.append((mark.name, item))
            if nom.items and not dated:
                # Дат нет ни у одной позиции первой же марки — значит поле не отдаётся.
                # Молча вернуть «новых нет» нельзя: это читается как «всё в порядке».
                return ("1С не отдаёт дату создания товаров — отобрать новые нечем. "
                        "Нужна выкладка обновлённого specs/1c/by-tm.bsl.")

        if not fresh:
            return (f"Новых товаров с {since} нет — проверять нечего "
                    f"(марок просмотрено {len(marks)}).")

        checker = photos.PhotoChecker()
        try:
            state = await asyncio.to_thread(checker.statuses,
                                            [i.id for _, i in fresh])
        finally:
            checker.close()

        rows, no_card, failed = [], 0, 0
        for tm_name, item in fresh:
            verdict = state.get((item.id or "").strip(), photos.NO_CARD)
            if verdict == photos.NONE:
                rows.append(photo_report.Row(
                    tm=tm_name, collection=nz.collection_of(item),
                    name=item.site_name or item.name, url=photos.item_url(item.id),
                    created=item.created))
            elif verdict == photos.NO_CARD:
                no_card += 1
            elif verdict == photos.FAILED:
                failed += 1

        text = photo_report.render(rows, since=since, checked=len(fresh),
                                   marks=len(marks), no_card=no_card, failed=failed,
                                   scope=wanted)
        return self._deliver(text, rows, since, lost)

    def _deliver(self, text: str, rows: list, since: str, lost: int) -> str:
        """Список — менеджеру напрямую, модели — только числа (см. `take_pending`)."""
        tail = (f" 1С не отдала {lost} поз. — они не проверены." if lost else "")
        if not rows:
            self.pending_message = None
            return text + tail

        if photo_report.fits_chat(text):
            self.pending_message = text
            how = "списком в чат"
        else:
            import tempfile
            path = Path(tempfile.gettempdir()) / f"Без фото {date.today():%d.%m.%Y}.xlsx"
            self.pending_file = photo_report.to_excel(rows, path)
            how = "файлом Excel (в чат не влез)"

        return (f"Найдено {len(rows)} товаров без фото среди заведённых с {since}. "
                f"Список уже отправлен менеджеру {how} — не пересказывай его." + tail)

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
        active = await self._exclusives()

        def exc_of(sample) -> object | None:
            return find(active, tm.code, sample.collection_ref, sample.ref)

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
                return finish(describe_product(found[0], sources, exc_of(found[0])))
            if len(found) <= 5:
                return finish("Подходит несколько товаров:\n" + "\n".join(
                    describe_product(i, sources, exc_of(i)) for i in found))
            return finish(describe_group(f"«{product}» у {tm.name}", found, sources,
                                        exc_of(found[0])))

        if collection:
            title = items[0].collection or items[0].parent or collection
            return finish(describe_group(f"{tm.name} {title}", items, sources,
                                        exc_of(items[0])))

        # запрос по ТМ целиком — разбираем по коллекциям в той же логике (п.6 ТЗ)
        groups: dict[str, list] = {}
        for item in items:
            groups.setdefault(item.collection_ref or item.collection or "", []).append(item)
        lines = [f"{tm.name}: {len(items)} товаров в {len(groups)} коллекциях."]
        ordered = sorted(groups.values(), key=lambda g: (g[0].collection or g[0].parent or ""))
        for group in ordered[:40]:
            title = group[0].collection or group[0].parent or "без коллекции"
            lines.append("• " + describe_group(title, group, sources, exc_of(group[0])))
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
