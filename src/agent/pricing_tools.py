"""Инструменты режима «обновление цен» (админский сценарий, specs/content-manager.md).

Все инструменты здесь — ЧТЕНИЕ 1С и подготовка предложения. Записи цен среди них нет
намеренно: payload сохраняется в `pending_proposal`, а отправляет его в 1С только
обработчик кнопки подтверждения (§10 спеки). Модель физически не может записать цены.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from src.onec.client import NomItem, OnecClient
from src.price_tool.changes import GroupResult, build_payload, plan_collection
from src.price_tool.parser import parse_price_table, render_preview
from src.price_tool.signature import price_signature
from src.storage.pricing import PricingStore

logger = logging.getLogger(__name__)

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
        self._nom: dict[str, list[NomItem]] = {}         # кэш номенклатуры по коду ТМ
        self.last_summary: str | None = None

    def set_file(self, filename: str, content: bytes) -> None:
        self._file = (filename, content)

    def handles(self, name: str) -> bool:
        return name in {t["name"] for t in PRICING_TOOLS}

    async def execute(self, name: str, inp: dict) -> str:
        try:
            if name == "read_price_file":
                return await self._read_with_mapping(inp)
            if name == "save_price_mapping":
                return await self._save_mapping(inp)
            if name == "get_selling_tm":
                return await asyncio.to_thread(self._selling_tm)
            if name == "get_1c_nomenclature":
                return await asyncio.to_thread(self._nomenclature, inp)
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

    async def _read_with_mapping(self, inp: dict) -> str:
        text = await asyncio.to_thread(self._read_price_file, inp)
        signature = await asyncio.to_thread(self._signature)
        if not signature:
            return text
        known = await self._store.get_mapping(signature)
        if not known:
            return (text + "\n\n[Формат прайса встречается впервые: трактовку колонок нужно "
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
        return (text + "\n\n[ЗАПОМНЕННЫЙ МАППИНГ этого формата прайса"
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

    def _read_price_file(self, inp: dict) -> str:
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
        text = "\n\n".join(render_preview(s) for s in sheets)
        return text[:40000]

    def _selling_tm(self) -> str:
        tms = self._onec.selling_tm()
        return json.dumps([{"name": t.name, "code": t.code} for t in tms], ensure_ascii=False)

    def _items(self, tm_code: str) -> list[NomItem]:
        if tm_code not in self._nom:
            self._nom[tm_code] = self._onec.by_tm_all(tm_code)
        return self._nom[tm_code]

    def _nomenclature(self, inp: dict) -> str:
        page, size = int(inp.get("page", 1)), int(inp.get("size", 200))
        items = self._items(inp["tm_code"])
        chunk = items[(page - 1) * size: page * size]
        return json.dumps({
            "tm": inp["tm_code"], "total": len(items), "page": page,
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

        for g in inp.get("groups", []):
            items = await asyncio.to_thread(self._items, g["tm_code"])
            sel = [i for i in items if i.collection_ref == g.get("collection_ref")]
            if not sel:
                problems.append(f"коллекция {g.get('collection_ref')} не найдена у ТМ {g['tm_code']}")
                continue
            results.append(plan_collection(
                sel, g["tm_code"], g.get("tm_name", ""),
                _dec(g.get("purchase")), _dec(g.get("rrc")), today))

        payload = build_payload(results)
        summary = self._render(inp, results, problems, payload)
        self.last_summary = summary

        if payload:
            await self._store.save_proposal(self._user_id, payload, summary)
            return (summary + "\n\n[Предложение сохранено. Покажи этот текст админу дословно "
                    "и жди нажатия кнопки — сам ничего не записывай.]")
        return summary + "\n\n[Записывать нечего — кнопка подтверждения не появится.]"

    def _render(self, inp: dict, results: list[GroupResult], problems: list[str],
                payload: list[dict]) -> str:
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
                lines.append(f"  • {g.collection} — {len(g.plans)} поз.")
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
