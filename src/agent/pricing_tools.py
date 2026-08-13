"""Инструменты режима «обновление цен» (админский сценарий, specs/content-manager.md).

Все инструменты здесь — ЧТЕНИЕ 1С и подготовка предложения. Записи цен среди них нет
намеренно: payload сохраняется в `pending_proposal`, а отправляет его в 1С только
обработчик кнопки подтверждения (§10 спеки). Модель физически не может записать цены.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from datetime import date
from decimal import Decimal, InvalidOperation

from src.onec.client import NomItem, Nomenclature, OnecClient
from src.price_tool.changes import GroupResult, build_payload, plan_collection
from src.price_tool.exclusive import (
    HINT_WORDS, WHERE_FOUND, annotate, find, resolve,
)
from src.price_tool.parser import extract_images, parse_price_table, render_preview
from src.price_tool.signature import price_signature
from src.storage.pricing import PricingStore

logger = logging.getLogger(__name__)

# Картинки едут в истории диалога (SQLite + каждый следующий запрос к модели), поэтому
# лимиты жёсткие: логотипы весят десятки килобайт, всё крупное — это фото товаров.
MAX_TEXT_CHARS = 40000
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 1_500_000
MAX_IMAGE_TOTAL_BYTES = 4_000_000

# Номенклатура ТМ живёт дольше одного хода диалога: PricingTools создаётся заново на
# каждое сообщение админа, и без общего кэша каждый его ответ («плинтус не трогай»)
# заново выкачивал бы из 1С все ТМ прайса — минуты молчания на мультибрендовом прайсе.
# Кэш сбрасывается при новом прайсе и после записи цен (см. clear_nomenclature_cache).
NOM_CACHE_TTL = 1800
_NOM_CACHE: dict[str, tuple[float, list]] = {}


def clear_nomenclature_cache() -> None:
    _NOM_CACHE.clear()

PRICING_TOOLS = [
    {
        "name": "read_price_file",
        "description": (
            "Читает присланный админом файл прайса (xlsx/xls/csv/pdf) и возвращает его "
            "таблицей. Параметр sheet — подстрока имени листа (для многолистовых прайсов; "
            "без него вернутся все листы). Вызывай в начале работы с прайсом."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sheet": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "save_price_mapping",
        "description": (
            "Запомнить трактовку колонок этого формата прайса, чтобы в следующий раз не "
            "спрашивать админа заново. Вызывай СРАЗУ ПОСЛЕ того, как админ ответил на "
            "вопрос о колонках (какая закупка, какая РРЦ, цена за м² или за упаковку). "
            "Привязка идёт к структуре файла, а не к имени файла: следующий прайс того же "
            "поставщика в том же формате подхватит её автоматически."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier": {"type": "string", "description": "поставщик — для показа админу"},
                "purchase_column": {"type": "string", "description": "заголовок колонки закупки, дословно"},
                "rrc_column": {"type": "string", "description": "заголовок колонки РРЦ; опустить, если её нет"},
                "basis": {"type": "string", "description": "base_unit (цена за базовую ЕИ) или package (за упаковку)"},
                "sheet": {"type": "string", "description": "лист с таблицей цен, если листов несколько"},
                "note": {"type": "string", "description": "чем именно был обусловлен выбор (слова админа)"},
            },
            "required": ["purchase_column"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_selling_tm",
        "description": (
            "Торговые марки, выгружаемые на сайт: [{name, code}]. Бренд прайса, которого "
            "здесь нет, не обрабатываем — только сообщаем админу. Без параметров."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_1c_nomenclature",
        "description": (
            "Номенклатура одной ТМ с текущими ценами (закупка/розница/РРЦ) и коэффициентами "
            "ЕИ. Параметры: tm_code (код из get_selling_tm), page (с 1), size (200). "
            "У товара есть collection_ref — код папки-коллекции, он нужен для предложения."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tm_code": {"type": "string"},
                "page": {"type": "integer"},
                "size": {"type": "integer"},
            },
            "required": ["tm_code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "record_exclusives",
        "description": (
            "Запомнить, что поставщик заявил в прайсе ЭКСКЛЮЗИВ на коллекцию или товар "
            "(«эксклюзив», «эксклюзивный дистрибьютор», «только у нас», «exclusive»). "
            "Вызывай ПОСЛЕ сопоставления с 1С и ДО propose_prices, если такие надписи в "
            "прайсе есть. Пометка чисто справочная — на цены она не влияет.\n"
            "ВАЖНО: слово, входящее в САМО НАЗВАНИЕ товара или коллекции (коллекция "
            "«Exclusive», «Kronotex Exclusive»), заявкой НЕ является — это имя, а не "
            "заявление о правах. Засчитывай, только если надпись стоит ОТДЕЛЬНО: в "
            "колонке-примечании, в строке-заголовке раздела, в имени листа или файла."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier": {"type": "string", "description": "поставщик, чей это прайс"},
                "price_date": {"type": "string", "description": "дата прайса ГГГГ-ММ-ДД; без неё — сегодняшняя"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tm_code": {"type": "string"},
                            "tm_name": {"type": "string"},
                            "collection_ref": {"type": "string", "description": "код папки-коллекции; опустить, если эксклюзив на всю ТМ"},
                            "collection": {"type": "string"},
                            "item_ref": {"type": "string", "description": "код товара, если эксклюзив на одну позицию"},
                            "item_name": {"type": "string"},
                            "phrase": {"type": "string", "description": "надпись из прайса ДОСЛОВНО"},
                            "where_found": {"type": "string", "description": "column | header | sheet | filename | admin"},
                        },
                        "required": ["tm_code", "phrase", "where_found"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["supplier", "items"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_exclusive",
        "description": (
            "Записать решение админа по спорному эксклюзиву (когда несколько поставщиков "
            "заявили его на одно и то же). Вызывай ТОЛЬКО после явного ответа админа. "
            "supplier — кого он выбрал; чтобы снять пометку совсем, передай supplier = "
            "\"none\"."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tm_code": {"type": "string"},
                "collection_ref": {"type": "string"},
                "item_ref": {"type": "string"},
                "supplier": {"type": "string", "description": "выбранный поставщик либо \"none\""},
                "note": {"type": "string", "description": "причина словами админа, если назвал"},
            },
            "required": ["tm_code", "supplier"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_prices",
        "description": (
            "Подготовить предложение по обновлению цен и показать его админу. Передай "
            "сопоставленные коллекции с ценами ИЗ ПРАЙСА, приведёнными к базовой ЕИ товара. "
            "Розницу НЕ передавай — она считается автоматически по правилам магазина. "
            "Инструмент сам сравнит с текущими ценами 1С, отбросит изменения меньше 2%, "
            "посчитает розницу и вернёт готовый текст предложения. Ничего не записывает: "
            "запись выполнит админ кнопкой. Вызывай ОДИН раз, когда сопоставление закончено."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier": {"type": "string", "description": "поставщик, как его назвал админ или как в прайсе"},
                "price_doc": {"type": "string", "description": "как назвать прайс менеджерам, напр. «Монарх-логистик»; по умолчанию имя файла"},
                "price_date": {"type": "string", "description": "дата самого прайса ГГГГ-ММ-ДД, если она видна в шапке или имени файла"},
                "groups": {
                    "type": "array",
                    "description": "по одной записи на коллекцию 1С",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tm_code": {"type": "string"},
                            "tm_name": {"type": "string"},
                            "collection_ref": {"type": "string", "description": "код папки-коллекции из get_1c_nomenclature"},
                            "purchase": {"type": "number", "description": "закупка из прайса за базовую ЕИ; опустить, если в прайсе нет"},
                            "rrc": {"type": "number", "description": "РРЦ из прайса за базовую ЕИ; опустить, если в прайсе нет"},
                            "note": {"type": "string"},
                        },
                        "required": ["tm_code", "collection_ref"],
                        "additionalProperties": False,
                    },
                },
                "warnings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "что показать админу: не сопоставленные строки, бренды не в выгрузке, расхождения",
                },
            },
            "required": ["groups"],
            "additionalProperties": False,
        },
    },
]


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _fmt(v) -> str:
    if v is None:
        return "—"
    d = Decimal(str(v))
    s = f"{d:,.0f}" if d == d.to_integral_value() else f"{d:,.2f}"
    return s.replace(",", " ")


def _pct(old, new) -> str:
    if not old:
        return "впервые"
    return f"{(Decimal(str(new)) - Decimal(str(old))) / Decimal(str(old)) * 100:+.1f}%"


def _transitions(group: GroupResult, kind: str, label: str) -> list[str]:
    """«было → стало», сгруппированное по одинаковому переходу."""
    buckets: dict[tuple, int] = {}
    for p in group.plans:
        if kind in p.prices:
            key = (p.before.get(kind), p.prices[kind])
            buckets[key] = buckets.get(key, 0) + 1
    out = []
    for (old, new), n in buckets.items():
        out.append(f"{label} {_fmt(old)} → {_fmt(new)} ({_pct(old, new)}), {n} поз.")
    return out


class PricingTools:
    """Исполнитель инструментов режима цен. Живёт в рамках одного пользователя."""

    def __init__(self, onec: OnecClient, store: PricingStore, user_id: int) -> None:
        self._onec = onec
        self._store = store
        self._user_id = user_id
        self._file: tuple[str, bytes] | None = None      # (имя, содержимое) текущего прайса
        self._images: list[dict] = []                    # баннеры из прайса — для модели
        self.last_summary: str | None = None

    def set_file(self, filename: str, content: bytes) -> None:
        self._file = (filename, content)

    def handles(self, name: str) -> bool:
        return name in {t["name"] for t in PRICING_TOOLS}

    async def execute(self, name: str, inp: dict) -> str | list[dict]:
        try:
            if name == "read_price_file":
                return await self._read_with_mapping(inp)
            if name == "save_price_mapping":
                return await self._save_mapping(inp)
            if name == "get_selling_tm":
                return await asyncio.to_thread(self._selling_tm)
            if name == "get_1c_nomenclature":
                return await asyncio.to_thread(self._nomenclature, inp)
            if name == "record_exclusives":
                return await self._record_exclusives(inp)
            if name == "set_exclusive":
                return await self._set_exclusive(inp)
            if name == "propose_prices":
                return await self._propose(inp)
            return f"Неизвестный инструмент: {name}"
        except Exception as exc:                       # noqa: BLE001
            logger.exception("Ошибка инструмента %s", name)
            return f"Ошибка выполнения {name}: {exc}"

    # ------------------------------------------------------------------ чтение

    def _signature(self) -> str:
        """Сигнатура структуры текущего прайса — ключ маппинга (§6.5.2)."""
        if not self._file:
            return ""
        filename, content = self._file
        sheets = parse_price_table(content, filename)
        if sheets:
            return price_signature(sheets)
        from src.email_tool.attachments import extract_text
        return price_signature([], extract_text(filename, content))

    async def _read_with_mapping(self, inp: dict) -> str | list[dict]:
        text = await asyncio.to_thread(self._read_price_file, inp)
        signature = await asyncio.to_thread(self._signature)
        if not signature:
            return self._attach(text)
        known = await self._store.get_mapping(signature)
        if not known:
            return self._attach(
                text + "\n\n[Формат прайса встречается впервые: трактовку колонок нужно "
                "определить, при неоднозначности — спросить админа и сохранить "
                "через save_price_mapping.]")
        m = known["mapping"]
        parts = [f"закупка = «{m.get('purchase_column')}»"]
        if m.get("rrc_column"):
            parts.append(f"РРЦ = «{m['rrc_column']}»")
        if m.get("basis"):
            parts.append(f"база цены: {m['basis']}")
        if m.get("sheet"):
            parts.append(f"лист: «{m['sheet']}»")
        note = f" Основание: {m['note']}." if m.get("note") else ""
        return self._attach(text + "\n\n[ЗАПОМНЕННЫЙ МАППИНГ этого формата прайса"
                + (f" (поставщик: {known['supplier']}" if known.get("supplier") else "")
                + f", сохранён {known['updated_at'][:10]}): "
                + "; ".join(parts) + f".{note} "
                "Применяй его и НЕ переспрашивай админа. Если админ явно скажет иначе — "
                "сохрани новую трактовку через save_price_mapping.]")

    async def _save_mapping(self, inp: dict) -> str:
        signature = await asyncio.to_thread(self._signature)
        if not signature:
            return "Не удалось вычислить сигнатуру прайса — маппинг не сохранён."
        mapping = {k: v for k, v in inp.items() if k != "supplier" and v}
        await self._store.save_mapping(signature, inp.get("supplier"), mapping)
        return ("Маппинг сохранён: следующий прайс этого формата будет разобран без "
                "вопросов о колонках.")

    # -------------------------------------------------------------- эксклюзивы (§9.5)

    async def _record_exclusives(self, inp: dict) -> str:
        """Заявки об эксклюзиве из прайса + сообщение о спорах, если они возникли."""
        supplier = (inp.get("supplier") or "").strip()
        if not supplier:
            return "Не указан поставщик — заявка об эксклюзиве не записана."
        price_date = (inp.get("price_date") or "")[:10] or date.today().isoformat()

        claims, in_name, bad_place = [], [], []
        for item in inp.get("items") or []:
            name = f"{item.get('collection') or ''} {item.get('item_name') or ''}".lower()
            if any(word in name for word in HINT_WORDS):
                # «Kronotex Exclusive» — это имя коллекции, а не заявление о правах;
                # принять такую заявку значит навесить ложный эксклюзив на весь бренд
                in_name.append(item.get("item_name") or item.get("collection") or "?")
                continue
            if item.get("where_found") not in WHERE_FOUND:
                bad_place.append(item.get("item_name") or item.get("collection") or "?")
                continue
            claims.append({**item, "supplier": supplier, "price_date": price_date})

        saved = await self._store.record_exclusive_claims(claims)
        active, disputes = resolve(*await self._store.load_exclusives())

        lines = [f"Заявки об эксклюзиве записаны: {saved} (из {len(inp.get('items') or [])})."]
        if in_name:
            lines.append(f"Пропущено — слово входит в само название ({len(in_name)}): "
                         + ", ".join(in_name[:5])
                         + ". Это имя коллекции, а не заявка. Если надпись всё-таки "
                           "стоит отдельно, спроси админа и вызови set_exclusive.")
        if bad_place:
            lines.append(f"Пропущено — не указано, где найдена надпись ({len(bad_place)}): "
                         + ", ".join(bad_place[:5]) + f". Ожидается одно из: {', '.join(WHERE_FOUND)}.")
        for d in disputes:
            lines.append(
                f"⚠️ СПОР: на «{d.title}» эксклюзив заявили несколько поставщиков — "
                + ", ".join(d.suppliers)
                + ". Пометка не показывается, пока админ не выберет. Процитируй ему "
                  "надписи из прайсов ("
                + "; ".join(f"{c.supplier}: «{c.phrase}»" for c in d.claims)
                + f"), спроси, за кем эксклюзив, и вызови set_exclusive (tm_code="
                + f"{d.tm_code}, collection_ref={d.collection_ref or '—'}).")
        if not disputes and saved:
            lines.append("Спорных нет — пометка будет показываться в отчётах.")
        return "\n".join(lines)

    async def _set_exclusive(self, inp: dict) -> str:
        raw = (inp.get("supplier") or "").strip()
        supplier = None if raw.lower() in {"none", "нет", "-", ""} else raw
        await self._store.set_exclusive_decision(
            inp["tm_code"], inp.get("collection_ref") or "", inp.get("item_ref") or "",
            supplier, inp.get("note"))
        if supplier:
            return f"Записано: эксклюзив за «{supplier}». Спрашивать об этом больше не буду."
        return "Записано: эксклюзива нет, пометка снята."

    def _read_price_file(self, inp: dict) -> str:
        """Текст прайса; найденные картинки складывает в self._images (см. _attach)."""
        self._images = []
        if not self._file:
            return "Файл прайса не приложен. Попроси админа прислать файл документом."
        filename, content = self._file
        sheets = parse_price_table(content, filename)
        if not sheets:
            from src.email_tool.attachments import extract_text
            text = extract_text(filename, content)
            return text[:40000] if text else "Не удалось разобрать файл."
        wanted = inp.get("sheet")
        if wanted:
            picked = [s for s in sheets if wanted.lower() in s.name.lower()]
            sheets = picked or sheets

        # Баннер бренда в прайсе часто вставлен картинкой: в тексте на его месте пусто,
        # и раздел другой ТМ выглядит продолжением предыдущей. Ставим маркер и прикладываем
        # само изображение — прочитать логотип может только модель.
        self._images = self._collect_images(content, filename, sheets)
        text = "\n\n".join(render_preview(s) for s in sheets)[:MAX_TEXT_CHARS]
        # текст обрезается по лимиту, и маркер картинки может в него не попасть —
        # тогда изображение приложить не к чему: модель получит его без объяснения
        self._images = [i for i in self._images if f"ИЗОБРАЖЕНИЕ #{i['n']}" in text]
        return text

    def _collect_images(self, content: bytes, filename: str, sheets: list) -> list[dict]:
        if not filename.lower().endswith(".xlsx"):
            return []
        try:
            by_sheet = extract_images(content)
        except Exception:
            logger.warning("Не удалось извлечь изображения прайса", exc_info=True)
            return []
        if not by_sheet:
            return []

        by_name = {s.name: s for s in sheets}
        found: list[dict] = []
        for name, images in by_sheet.items():
            if name in by_name:
                for row, data, media_type in images:
                    found.append({"row": row, "sheet": name, "data": data,
                                  "media_type": media_type})
        found.sort(key=lambda i: (i["sheet"], i["row"]))

        # Один и тот же логотип часто вставлен десятками копий (в прайсе Монарха — 93
        # копии одной картинки, 2.7 МБ). Прикладываем каждую РАЗНУЮ картинку один раз,
        # повторы только ссылаются на неё: иначе лимит съедается копиями одной и той же.
        seen: dict[bytes, dict] = {}
        attached: list[dict] = []
        budget = MAX_IMAGE_TOTAL_BYTES
        for image in found:
            digest = hashlib.sha1(image["data"]).digest()
            first = seen.get(digest)
            if first is not None:
                n = first.get("n")
                image["label"] = (
                    f"⟨ИЗОБРАЖЕНИЕ #{n} ещё раз — та же картинка, что у строки "
                    f"{first['row']}⟩" if n else
                    f"⟨ИЗОБРАЖЕНИЕ — повтор картинки у строки {first['row']}, "
                    "она не приложена⟩")
                continue
            seen[digest] = image
            size = len(image["data"])
            if size > MAX_IMAGE_BYTES:
                image["label"] = (f"⟨ИЗОБРАЖЕНИЕ у строки {image['row']} — {size // 1024} КБ, "
                                  "слишком большое, не приложено⟩")
            elif len(attached) >= MAX_IMAGES or size > budget:
                image["label"] = (f"⟨ИЗОБРАЖЕНИЕ у строки {image['row']} — не приложено, "
                                  f"исчерпан лимит в {MAX_IMAGES} картинок на прайс⟩")
            else:
                budget -= size
                image["n"] = len(attached) + 1
                attached.append(image)
                # Строка — это ЯКОРЬ (левый верхний угол) картинки: визуально она может
                # накрывать и соседние строки, поэтому точную границу раздела модель
                # должна определять по содержимому, а не по позиции маркера.
                image["label"] = (
                    f"⟨ИЗОБРАЖЕНИЕ #{image['n']}, привязано к строке {image['row']} "
                    f"листа «{image['sheet']}» — приложено к этому же результату. "
                    "Картинка может перекрывать соседние строки: точную границу "
                    "раздела определи по заголовкам коллекций и формату артикулов⟩")

        # маркеры вставляем с конца, чтобы не сдвинуть ещё не обработанные строки
        for image in sorted(found, key=lambda i: i["row"], reverse=True):
            sheet = by_name[image["sheet"]]
            idx = min(max(image["row"] - 1, 0), len(sheet.rows))
            sheet.rows.insert(idx, [image["label"]])
        return attached

    def _attach(self, text: str) -> str | list[dict]:
        """Результат инструмента: текст + картинки блоками, если они есть."""
        if not self._images:
            return text
        blocks: list[dict] = [{"type": "text", "text": text}]
        for image in self._images:
            blocks.append({"type": "text",
                           "text": f"Изображение #{image['n']} "
                                   f"(лист «{image['sheet']}», строка {image['row']}):"})
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": image["media_type"],
                "data": base64.b64encode(image["data"]).decode("ascii")}})
        return blocks

    def _selling_tm(self) -> str:
        tms = self._onec.selling_tm()
        return json.dumps([{"name": t.name, "code": t.code} for t in tms], ensure_ascii=False)

    def _nom(self, tm_code: str) -> Nomenclature:
        hit = _NOM_CACHE.get(tm_code)
        if hit and time.monotonic() - hit[0] < NOM_CACHE_TTL:
            return hit[1]
        started = time.monotonic()
        nom = self._onec.by_tm_all(tm_code)
        logger.info("Номенклатура ТМ %s: %d поз. за %.1f c%s", tm_code, len(nom.items),
                    time.monotonic() - started,
                    f", НЕ ОТДАНО 1С: {len(nom.errors)}" if nom.errors else "")
        _NOM_CACHE[tm_code] = (time.monotonic(), nom)
        return nom

    def _items(self, tm_code: str) -> list[NomItem]:
        return self._nom(tm_code).items

    def _nomenclature(self, inp: dict) -> str:
        page, size = int(inp.get("page", 1)), int(inp.get("size", 200))
        nom = self._nom(inp["tm_code"])
        items = nom.items
        chunk = items[(page - 1) * size: page * size]
        return json.dumps({
            "tm": inp["tm_code"], "total": len(items), "page": page,
            # позиции, которые 1С не смогла отдать: их не будет в items, и молчать
            # об этом нельзя — сопоставление с прайсом окажется неполным
            "not_returned_by_1c": [
                {"ref": e.get("ref"), "code": e.get("code"), "message": e.get("message")}
                for e in nom.errors[:20]],
            "items": [{
                "ref": i.ref, "name": i.name, "article": i.article, "size": i.size,
                "collection": i.collection, "collection_ref": i.collection_ref,
                "product_type": i.product_type, "unit": i.unit, "alt_units": i.alt_units,
                "purchase": i.purchase.value if i.purchase else None,
                "retail": i.retail.value if i.retail else None,
                "rrc": i.rrc.value if i.rrc else None,
            } for i in chunk],
        }, ensure_ascii=False)

    # -------------------------------------------------------------- предложение

    async def _propose(self, inp: dict) -> str:
        today = date.today()
        results: list[GroupResult] = []
        problems: list[str] = []

        seen_tm: set[str] = set()
        for g in inp.get("groups", []):
            nom = await asyncio.to_thread(self._nom, g["tm_code"])
            # 1С может не отдать часть позиций (см. specs/1c/by-tm.bsl): они не попадут
            # в сопоставление, и админ должен увидеть это в предложении, а не догадываться
            if nom.errors and g["tm_code"] not in seen_tm:
                refs = ", ".join(str(e.get("ref")) for e in nom.errors[:5] if e.get("ref"))
                problems.append(
                    f"⚠️ 1С не отдала {len(nom.errors)} поз. по ТМ "
                    f"{g.get('tm_name') or g['tm_code']} — они НЕ проверены по прайсу"
                    + (f" ({refs}{', …' if len(nom.errors) > 5 else ''})" if refs else ""))
            seen_tm.add(g["tm_code"])
            items = nom.items
            sel = [i for i in items if i.collection_ref == g.get("collection_ref")]
            if not sel:
                problems.append(f"коллекция {g.get('collection_ref')} не найдена у ТМ {g['tm_code']}")
                continue
            results.append(plan_collection(
                sel, g["tm_code"], g.get("tm_name", ""),
                _dec(g.get("purchase")), _dec(g.get("rrc")), today))

        payload = build_payload(results)
        active, _ = resolve(*await self._store.load_exclusives())
        summary = self._render(inp, results, problems, payload, active)
        self.last_summary = summary

        if payload:
            await self._store.save_proposal(self._user_id, payload, summary,
                                            digest=self._digest(inp, results))
            return (summary + "\n\n[Предложение сохранено. Покажи этот текст админу дословно "
                    "и жди нажатия кнопки — сам ничего не записывай.]")
        return summary + "\n\n[Записывать нечего — кнопка подтверждения не появится.]"

    def _digest(self, inp: dict, results: list[GroupResult]) -> dict:
        """Снимок «было → стало» по товарам — для рассылки менеджерам и журнала (п.6).

        Сохраняется вместе с payload: после нажатия кнопки пересчитать его уже неоткуда,
        а «было» есть только на нашей стороне.
        """
        return {
            "supplier": inp.get("supplier"),
            "price_doc": inp.get("price_doc") or (self._file[0] if self._file else None),
            "price_date": inp.get("price_date"),
            "groups": [{
                "tm_code": g.tm_code, "tm_name": g.tm_name,
                "collection": g.collection, "collection_ref": g.collection_ref,
                "items": [{
                    "ref": p.ref, "name": p.name,
                    "prices": {k: [None if p.before.get(k) is None else float(p.before[k]),
                                   float(v)] for k, v in p.prices.items()},
                } for p in g.to_write],
            } for g in results if g.to_write],
        }

    def _render(self, inp: dict, results: list[GroupResult], problems: list[str],
                payload: list[dict], exclusives: dict | None = None) -> str:
        supplier = inp.get("supplier") or "поставщика"
        lines = [f"Обновляем цены от {supplier} на:"]
        by_tm: dict[str, list[GroupResult]] = {}
        for g in results:
            by_tm.setdefault(g.tm_name or g.tm_code, []).append(g)

        total_items = 0
        for tm_name, groups in by_tm.items():
            lines.append(f"— {tm_name}")
            untouched: list[str] = []
            for g in groups:
                rows = (_transitions(g, "purchase", "закупка")
                        + _transitions(g, "rrc", "РРЦ")
                        + _transitions(g, "retail", "розница"))
                if not rows:
                    # коллекции без изменений не перечисляем по одной: в прайсе их
                    # обычно большинство, и они прячут собой то, что реально меняется
                    untouched.append(g.collection)
                    continue
                total_items += len(g.to_write)
                title = annotate(g.collection,
                                 find(exclusives or {}, g.tm_code, g.collection_ref))
                lines.append(f"  • {title} — {len(g.plans)} поз.")
                for r in rows:
                    lines.append(f"      {r}")
            if untouched:
                shown = ", ".join(untouched[:6])
                tail = f" и ещё {len(untouched) - 6}" if len(untouched) > 6 else ""
                lines.append(f"  • без изменений ({len(untouched)}): {shown}{tail}")
        lines.append("?")

        warns = list(inp.get("warnings") or []) + problems
        for g in results:
            below_p = [p for p in g.plans if p.warning == "rrc_below_purchase"]
            below_r = [p for p in g.plans if p.warning == "rrc_below_retail"]
            skipped = sum(1 for p in g.plans if "below_threshold" in p.skipped.values())
            if below_p:
                warns.append(f"⚠️ {g.collection}: РРЦ ниже закупки — проверьте прайс ({len(below_p)} поз.)")
            if below_r:
                warns.append(f"⚠️ {g.collection}: наша розница выше РРЦ ({len(below_r)} поз.) — цену не меняю")
            if skipped:
                warns.append(f"ℹ️ {g.collection}: {skipped} поз. пропущено — изменение меньше 2%")
        if warns:
            lines.append("")
            lines.extend(warns)

        lines.append("")
        lines.append(f"К записи: {total_items} поз. ({len(payload)} строк запроса в 1С).")
        return "\n".join(lines)
