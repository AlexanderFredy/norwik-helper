"""Интеграционная проверка: парсинг прайсов из почты + сопоставление с номенклатурой 1С.

Read-only: читает почту (IMAP peek), тянет 1С (selling-tm / by-tm), гоняет агента.
Ничего не пишет (set-prices не участвует). specs/content-manager.md §6–§9.

Запуск (из корня основного checkout, где лежит .env):
    .venv\\Scripts\\python -m tests.integration_price_match [--days 30] [--max 3]
    .venv\\Scripts\\python -m tests.integration_price_match --file prices/x.xlsx --brand Classen

Нужны в .env: MAIL_*, ANTHROPIC_API_KEY, ONEC_BASE_URL, ONEC_TOKEN.
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import anthropic

from src.config import load_config
from src.email_tool.attachments import excel_sheet_names
from src.email_tool.classifier import MailKind, classify
from src.email_tool.client import MailClient
from src.onec.client import NomItem, OnecClient
from src.price_tool.parser import parse_price_table, render_preview

MODEL = "claude-opus-4-8"
_PRICE_EXTS = (".xlsx", ".xls", ".csv")

SYSTEM_PROMPT = """Ты — контент-менеджер интернет-магазина. Задача: разобрать прайс поставщика
и сопоставить его позиции с номенклатурой 1С. Только чтение, ничего не записывай.

Шаги:
1. По содержимому прайса определи БРЕНД (торговую марку) и тип товара (product_type).
2. Вызови get_selling_tm и найди код (Code) этой ТМ по наименованию (NameTM бывает
   двуязычным «Latin / Кириллица» — сравнивай нормализованно). Если бренда нет в списке —
   сообщи, что ТМ не выгружается, и остановись по этому прайсу.
3. Вызови get_nomenclature с этим кодом (при необходимости листай страницы по total).
4. Определи в прайсе колонки: идентификация товара (артикул/наименование/коллекция/размер)
   и цены (закупка и/или РРЦ). Учти: цена может быть за упаковку → приведи к базовой ЕИ (unit).
5. Сопоставь строки прайса с товарами 1С: приоритет — точный артикул → размер+коллекция+декор
   → нечёткое по наименованию (учитывай отклонения написания, латиница/кириллица). Размер в
   формате Д×Ш×Т мм, длина/ширина могут быть диапазоном — это не расхождение.
6. Для каждой сопоставленной позиции сравни цену прайса с текущей ценой 1С (purchase/rrc).

Выведи отчёт (кратко, по-русски):
- Бренд, product_type, код ТМ, сколько позиций в прайсе / в номенклатуре 1С.
- Таблица: строка прайса → статус (confident/disputed/unmatched), ref, старая→новая цена
  (закупка/РРЦ), %; для disputed — кандидаты.
- Итог: сколько confident/disputed/unmatched, средний % по коллекциям, предупреждения
  (расхождение parent/collection, несколько product_type, непарсибельные цены).
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
        "unit": it.unit,
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


def match_pricelist(client, onec: OnecClient, price_text: str, meta: str) -> str:
    """Гоняет агента над одним прайсом, возвращает текстовый отчёт."""
    messages = [{"role": "user", "content": f"{meta}\n\nСодержимое прайса:\n{price_text}"}]
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
    ap.add_argument("--file", type=str, help="локальный файл прайса вместо почты")
    ap.add_argument("--brand", type=str, help="подсказка бренда для --file")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.onec_base_url or not cfg.onec_token:
        raise SystemExit("Заполните ONEC_BASE_URL и ONEC_TOKEN в .env")
    onec = OnecClient(cfg.onec_base_url, cfg.onec_token)
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    try:
        jobs = []  # (meta, price_text)
        if args.file:
            content = Path(args.file).read_bytes()
            sheets = parse_price_table(content, args.file)
            text = "\n\n".join(render_preview(s) for s in sheets) or "(не удалось разобрать)"
            meta = f"Файл: {args.file}" + (f"; бренд-подсказка: {args.brand}" if args.brand else "")
            jobs.append((meta, text))
        else:
            mail = MailClient(cfg.mail_host, cfg.mail_port, cfg.mail_user, cfg.mail_password)
            print(f"Ищу прайсы в почте за {args.days} дн...")
            emails = collect_price_emails(mail, args.days, args.max)
            print(f"Найдено прайсов: {len(emails)}")
            for full, att in emails:
                sheets = parse_price_table(att.content, att.filename)
                text = "\n\n".join(render_preview(s) for s in sheets) or "(не удалось разобрать)"
                meta = (f"От: {full.sender_name} <{full.sender_email}>; тема: {full.subject}; "
                        f"вложение: {att.filename}")
                jobs.append((meta, text))

        for i, (meta, text) in enumerate(jobs, 1):
            print(f"\n{'='*70}\n[{i}/{len(jobs)}] {meta}\n{'='*70}")
            report = match_pricelist(client, onec, text, meta)
            print(report)
    finally:
        onec.close()


if __name__ == "__main__":
    main()
