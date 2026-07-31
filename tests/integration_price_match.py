"""Интеграционная проверка: парсинг прайсов из почты + сопоставление с номенклатурой 1С.

Read-only: читает почту (IMAP peek), тянет 1С (selling-tm / by-tm), гоняет агента.
Ничего не пишет (set-prices не участвует). specs/content-manager.md §6–§9.

Запуск (из корня основного checkout, где лежит .env):
    .venv\\Scripts\\python -m tests.integration_price_match [--days 30] [--max 3]
    .venv\\Scripts\\python -m tests.integration_price_match --file prices/x.xlsx --brand Classen

Нужны в .env: MAIL_*, ANTHROPIC_API_KEY, ONEC_BASE_URL, ONEC_TOKEN.
"""
import argparse
import base64
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import anthropic

from src.config import load_config
from src.email_tool.attachments import excel_sheet_names, extract_text
from src.email_tool.classifier import MailKind, classify
from src.email_tool.client import MailClient
from src.onec.client import NomItem, OnecClient
from src.price_tool.parser import extract_images, parse_price_table, render_preview

MODEL = "claude-opus-4-8"
_PRICE_EXTS = (".xlsx", ".xls", ".csv", ".pdf")
_MAX_IMAGES = 8
_MAX_IMG_BYTES = 4_000_000


def price_to_text(content: bytes, filename: str, sheet: str | None = None,
                  max_chars: int = 40000) -> tuple[str, list[dict]]:
    """Текст прайса + встроенные изображения (для проверки баннеров-брендов).

    sheet — подстрока имени листа (фолбэк на все). Возвращает (текст, images), где images —
    [{n, row, sheet, bytes, media_type}]; в текст на позиции картинки вставлен маркер #n.
    """
    sheets = parse_price_table(content, filename)
    images: list[dict] = []
    if sheets:
        if sheet:
            picked = [s for s in sheets if sheet.lower() in s.name.lower()]
            sheets = picked or sheets
        by_sheet = extract_images(content) if filename.lower().endswith(".xlsx") else {}
        by_name = {s.name: s for s in sheets}
        for name in [s.name for s in sheets]:
            for (row, data, mtype) in by_sheet.get(name, []):
                images.append({"n": len(images) + 1, "row": row, "sheet": name,
                               "bytes": data, "media_type": mtype})
        # вставляем нумерованные маркеры (с конца — чтобы индексы не сдвигались)
        for img in sorted(images, key=lambda i: i["row"], reverse=True):
            s = by_name[img["sheet"]]
            idx = min(max(img["row"] - 1, 0), len(s.rows))
            s.rows.insert(idx, [f"⟨ИЗОБРАЖЕНИЕ #{img['n']} — см. приложенное изображение⟩"])
        text = "\n\n".join(render_preview(s) for s in sheets)
    else:
        text = extract_text(filename, content)  # pdf/docx фолбэк
    if not text:
        text = "(не удалось разобрать)"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... (обрезано, всего {len(text)} символов)"
    return text, images

SYSTEM_PROMPT = """Ты — контент-менеджер интернет-магазина. Задача: разобрать прайс поставщика
и сопоставить его позиции с номенклатурой 1С. Только чтение, ничего не записывай.

Шаги:
1. Определи ВСЕ бренды в прайсе (прайс бывает мультибрендовым) и тип товара (product_type).
   Бренды бывают: в колонке «Бренд»; в строках-заголовках разделов; в шапке/примечаниях.
   ПРО КАРТИНКИ: в тексте есть маркеры «⟨ИЗОБРАЖЕНИЕ #N⟩», а сами картинки приложены к
   сообщению. Наличие картинки САМО ПО СЕБЕ НЕ означает смену бренда — картинки бывают
   любые (фото товара, логотип, декор). ПОСМОТРИ на приложенное изображение: считай его
   разделителем нового бренда ТОЛЬКО если на нём явно написано НАЗВАНИЕ БРЕНДА (баннер).
   Тогда коллекции ПОСЛЕ маркера отнеси к этому бренду. Если на картинке нет названия
   бренда — игнорируй её как разделитель, бренд не меняется.
2. Вызови get_selling_tm ОДИН раз. Для КАЖДОГО бренда прайса найди код (Code) по
   наименованию (NameTM бывает двуязычным «Latin / Кириллица» — сравнивай нормализованно).
   Раздели бренды на: (а) есть в 1С → обрабатываем; (б) нет в selling-tm → только упоминаем.
3. Для КАЖДОГО бренда из (а) вызови get_nomenclature с его кодом (листай страницы по total)
   и собери расхождения. Бренды из (б) не запрашивай и не детализируй.
4. Определи в прайсе колонки: идентификация товара (артикул/наименование/коллекция/размер)
   и цены (закупка и/или РРЦ). Учти единицы измерения:
   - Цены в 1С — за БАЗОВУЮ ЕИ (поле `unit`, напр. м²). У товара есть `alt_units` —
     коэффициенты иных ЕИ к базовой, напр. {"упак": 2.367} = 1 упаковка = 2.367 м².
   - Если цена в прайсе дана за упаковку (или иную ЕИ) — приведи к базовой ЕИ, ДЕЛЯ на
     коэффициент из `alt_units` (цена_за_м² = цена_за_упак / 2.367), и только потом сравнивай.
   - СВЕРЬ фасовку: если в прайсе указана «кол-во в упаковке» (шт/м² в пачке), сравни её с
     коэффициентом `alt_units` из 1С. Если расходятся (напр. прайс 2,16 м²/пачка, а 1С 2,367)
     — вынеси в предупреждения (расхождение ЕИ/фасовки), это влияет на пересчёт цены.
5. Сопоставь строки прайса с товарами 1С: приоритет — точный артикул → размер+коллекция+декор
   → нечёткое по наименованию (учитывай отклонения написания, латиница/кириллица). Размер в
   формате Д×Ш×Т мм, длина/ширина могут быть диапазоном — это не расхождение.
6. Для каждой сопоставленной позиции сравни цену прайса с текущей ценой 1С (purchase/rrc).

ВАЖНО: расхождения собирай ТОЛЬКО по ТМ, которые реально есть в 1С (вернул get_selling_tm
и get_nomenclature). Бренды прайса, которых нет в selling-tm, лишь перечисли одной строкой
как «не выгружается» — без детализации позиций и расхождений.

Выведи отчёт в Markdown (по-русски). Если брендов несколько — раздел на каждую ТМ из 1С:
- Бренд(ы), product_type, код(ы) ТМ, сколько позиций в прайсе / в номенклатуре 1С.
- **Таблица расхождений цен** (только сопоставленные позиции ТМ из 1С, где цена прайса ≠
  текущей в 1С): наименование, ref, вид цены, старая→новая, %.
- Таблица сопоставления: строка прайса → статус (confident/disputed/unmatched), ref;
  для disputed — кандидаты.
- Итог: сколько confident/disputed/unmatched, средний % по коллекциям.
- Предупреждения: расхождение parent/collection, размеры, несколько product_type,
  непарсибельные цены, бренды не в selling-tm, расхождение ЕИ/фасовки (alt_units vs прайс).
"""

TOOLS = [
    {
        "name": "get_selling_tm",
        "description": "Список торговых марок, выгружаемых на сайт: [{name, code}]. Без параметров.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_nomenclature",
        "description": (
            "Номенклатура одной ТМ с ценами по коду ТМ. Параметры: tm_code (строка-код из "
            "get_selling_tm), page (с 1), size (по умолчанию 200). Возвращает total и items "
            "с полями ref/name/article/size/collection/parent/product_type/unit/purchase/rrc."
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
]


def _item_dict(it: NomItem) -> dict:
    return {
        "ref": it.ref, "name": it.name, "article": it.article, "size": it.size,
        "collection": it.collection, "parent": it.parent, "product_type": it.product_type,
        "unit": it.unit, "alt_units": it.alt_units,
        "purchase": {"value": it.purchase.value, "date": it.purchase.date} if it.purchase else None,
        "rrc": {"value": it.rrc.value, "date": it.rrc.date} if it.rrc else None,
    }


def _run_tool(onec: OnecClient, name: str, inp: dict) -> str:
    if name == "get_selling_tm":
        tms = onec.selling_tm()
        return json.dumps([{"name": t.name, "code": t.code} for t in tms], ensure_ascii=False)
    if name == "get_nomenclature":
        page = onec.by_tm(inp["tm_code"], page=inp.get("page", 1), size=inp.get("size", 200))
        return json.dumps(
            {"tm": page.tm, "total": page.total, "page": page.page, "size": page.size,
             "items": [_item_dict(i) for i in page.items]},
            ensure_ascii=False,
        )
    return f"Неизвестный инструмент: {name}"


def _image_blocks(images: list[dict]) -> list[dict]:
    """Image-блоки Anthropic для приложенных картинок прайса (с подписью строки)."""
    blocks = []
    for img in images:
        if len(img["bytes"]) > _MAX_IMG_BYTES or len(blocks) >= _MAX_IMAGES * 2:
            continue
        blocks.append({"type": "text",
                       "text": f"Приложенное изображение #{img['n']} (лист «{img['sheet']}», строка {img['row']}):"})
        blocks.append({"type": "image", "source": {
            "type": "base64", "media_type": img["media_type"],
            "data": base64.b64encode(img["bytes"]).decode()}})
    return blocks


def match_pricelist(client, onec: OnecClient, price_text: str, meta: str,
                    images: list[dict] | None = None) -> str:
    """Гоняет агента над одним прайсом, возвращает текстовый отчёт."""
    content = [{"type": "text", "text": f"{meta}\n\nСодержимое прайса:\n{price_text}"}]
    content += _image_blocks(images or [])
    messages = [{"role": "user", "content": content}]
    for _ in range(20):
        resp = client.messages.create(
            model=MODEL, max_tokens=8000,
            system=SYSTEM_PROMPT, tools=TOOLS, messages=messages,
        )
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not tool_uses:
            return "".join(b.text for b in resp.content if b.type == "text").strip()
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for t in tool_uses:
            print(f"    · {t.name}({', '.join(f'{k}={v}' for k, v in t.input.items())})")
            results.append({"type": "tool_result", "tool_use_id": t.id,
                            "content": _run_tool(onec, t.name, t.input)})
        messages.append({"role": "user", "content": results})
    return "(превышен лимит итераций агента)"


def _price_attachment(msg):
    for att in msg.attachments:
        if att.filename.lower().endswith(_PRICE_EXTS):
            return att
    return None


def collect_price_emails(mail: MailClient, days: int, want: int, inspect: int = 40):
    """Свежие письма-прайсы с Excel/CSV-вложением, от разных отправителей."""
    since = date.today() - timedelta(days=days)
    heads = mail.search(since=since, limit=inspect)
    chosen, seen_senders = [], set()
    for h in heads:
        if len(chosen) >= want:
            break
        if h.sender_email in seen_senders:
            continue
        full = mail.fetch_message(h.uid)
        att = _price_attachment(full)
        if att is None:
            continue
        sheets = excel_sheet_names(att.content) if att.filename.lower().endswith((".xlsx", ".xls")) else []
        if classify(full.subject, [att.filename], sheets) is not MailKind.PRICE:
            continue
        chosen.append((full, att))
        seen_senders.add(h.sender_email)
    return chosen


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--max", type=int, default=3)
    ap.add_argument("--file", type=str, help="локальный файл или папка прайсов вместо почты")
    ap.add_argument("--brand", type=str, help="подсказка бренда для --file")
    ap.add_argument("--sheet", type=str, help="подстрока имени листа (напр. 'SPC LVT')")
    ap.add_argument("--out", type=str, help="папка для файлов-отчётов (по одному на прайс)")
    ap.add_argument("--max-chars", type=int, default=40000, help="лимит текста прайса в промпт")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.onec_base_url or not cfg.onec_token:
        raise SystemExit("Заполните ONEC_BASE_URL и ONEC_TOKEN в .env")
    onec = OnecClient(cfg.onec_base_url, cfg.onec_token)
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    try:
        jobs = []  # (name, meta, price_text, images)
        if args.file:
            p = Path(args.file)
            files = sorted(f for f in p.glob("*") if f.is_file()) if p.is_dir() else [p]
            for f in files:
                text, images = price_to_text(f.read_bytes(), f.name, sheet=args.sheet, max_chars=args.max_chars)
                meta = f"Файл: {f.name}" + (f"; бренд-подсказка: {args.brand}" if args.brand else "")
                jobs.append((f.stem, meta, text, images))
        else:
            mail = MailClient(cfg.mail_host, cfg.mail_port, cfg.mail_user, cfg.mail_password)
            print(f"Ищу прайсы в почте за {args.days} дн...")
            emails = collect_price_emails(mail, args.days, args.max)
            print(f"Найдено прайсов: {len(emails)}")
            for full, att in emails:
                text, images = price_to_text(att.content, att.filename, sheet=args.sheet, max_chars=args.max_chars)
                meta = (f"От: {full.sender_name} <{full.sender_email}>; тема: {full.subject}; "
                        f"вложение: {att.filename}")
                jobs.append((Path(att.filename).stem, meta, text, images))

        for i, (name, meta, text, images) in enumerate(jobs, 1):
            print(f"\n{'='*70}\n[{i}/{len(jobs)}] {meta} | картинок: {len(images)}\n{'='*70}")
            report = match_pricelist(client, onec, text, meta, images)
            print(report)
            if out_dir:
                safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in name).strip()
                path = out_dir / f"{safe}.md"
                header = f"# Отчёт сопоставления: {name}\n\n_{meta}_"
                if args.sheet:
                    header += f"\n_Лист: {args.sheet}_"
                path.write_text(f"{header}\n\n{report}\n", encoding="utf-8")
                print(f"\n[отчёт сохранён: {path}]")
    finally:
        onec.close()


if __name__ == "__main__":
    main()
