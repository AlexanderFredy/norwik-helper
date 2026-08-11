"""Прогон трёх прайсов: что агент напишет админу в Telegram (без записи в 1С).

Модель делает только разбор и сопоставление (её результат — строгий JSON). Все цены,
розница, пороги и предупреждения считаются ДЕТЕРМИНИРОВАННО по specs/retail-price-rules.md
и §9 спеки — чтобы отчёт нельзя было «сочинить».

1С только читается (`by-tm`); `set-prices` НЕ вызывается: финальное подтверждение админа
в этом прогоне не даётся, вопросы остаются без ответа.

Запуск:
    .venv\\Scripts\\python -m tests.integration_tg_preview
    .venv\\Scripts\\python -m tests.integration_tg_preview --only Монарх --sheet "SPC LVT"

Результат — tests/out/tg_messages.md
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from datetime import date
from decimal import Decimal

import anthropic

from src.config import load_config
from src.onec.client import OnecClient, NomItem
from src.price_tool.retail import compute_retail
from tests.integration_price_match import (MODEL, TOOLS, _image_blocks, _item_dict,
                                           price_to_text, _run_tool)

PRICE_DIR = r"C:\Data\ClodeCodeProjects\shop-helper\.claude\test-prices"
OUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "specs", "tg-messages-preview.md")
SHEETS = {"Монарх": "SPC LVT"}          # у мультилистовых прайсов — нужный лист

EXTRACT_PROMPT = """Ты разбираешь прайс поставщика и сопоставляешь его с номенклатурой 1С.
Только чтение. Ответ — СТРОГО JSON по схеме ниже, без пояснений вокруг.

Шаги:
1. Определи бренды прайса (прайс бывает мультибрендовым: колонка «Бренд», строки-заголовки
   разделов, шапка). Маркер «⟨ИЗОБРАЖЕНИЕ #N⟩» — картинка приложена к сообщению: считай её
   разделителем нового бренда ТОЛЬКО если на ней написано НАЗВАНИЕ БРЕНДА.
2. Вызови get_selling_tm один раз, сопоставь бренды с ТМ 1С (NameTM бывает двуязычным).
3. Для брендов, которые есть в 1С, вызови get_nomenclature и сопоставь строки прайса с
   КОЛЛЕКЦИЯМИ 1С (поле collection_ref — код папки). Цены приведи к БАЗОВОЙ ЕИ товара
   (`unit`), деля на коэффициент из `alt_units`, если цена дана за упаковку.
4. Не выдумывай цены: бери из прайса. Если по коллекции в прайсе несколько разных цен —
   заведи отдельную группу на каждый типоразмер.
5. ВАЖНО про колонки: если под ОДИН вид цены подходит НЕСКОЛЬКО колонок (например
   «самовывоз» и «с доставкой», «от 20 упак.» и розничная опт), это НЕОДНОЗНАЧНОСТЬ —
   поставь "ambiguous": true и сформулируй вопрос админу: перечисли колонки-кандидаты с
   примерами значений и спроси, какую считать закупкой. Не выбирай молча.
6. Одна коллекция 1С (collection_ref) должна встречаться в groups ОДИН раз. Если строки
   прайса с разными ценами ложатся в одну коллекцию — оставь одну группу и опиши конфликт
   в note.

Схема ответа:
{
  "supplier_guess": "название компании-поставщика, как её видно в прайсе",
  "column_mapping": {"purchase": "заголовок колонки закупки",
                     "rrc": "заголовок колонки РРЦ или null",
                     "basis": "base_unit|package",
                     "ambiguous": true|false,
                     "question": "что именно неясно (если ambiguous), иначе null"},
  "brands": [
    {"brand": "как в прайсе", "in_1c": true|false, "tm_code": "код или null",
     "tm_name": "имя ТМ в 1С или null", "product_type": "тип товара",
     "groups": [
       {"price_row": "строка прайса/коллекция как в прайсе",
        "collection_ref": "код папки 1С", "collection_1c": "имя коллекции в 1С",
        "purchase": число|null, "rrc": число|null,
        "confidence": "confident|disputed", "note": "чем сопоставлено / что смущает"}
     ],
     "unmatched": [{"price_row": "...", "reason": "..."}]}
  ],
  "warnings": ["расхождения parent/collection, несколько product_type, битые строки и т.п."]
}"""


def _fmt(v) -> str:
    if v is None:
        return "—"
    d = Decimal(str(v))
    return f"{d:,.0f}".replace(",", " ") if d == d.to_integral_value() else f"{d:,.2f}".replace(",", " ")


def _pct(old, new) -> str:
    if not old:
        return ""
    p = (Decimal(str(new)) - Decimal(str(old))) / Decimal(str(old)) * 100
    return f"{p:+.1f}%"


def extract(client, onec: OnecClient, text: str, images: list[dict], meta: str) -> dict:
    """Прогон модели: разбор + сопоставление → JSON."""
    content = [{"type": "text", "text": f"{meta}\n\n{text}"}] + _image_blocks(images)
    messages = [{"role": "user", "content": content}]
    state: dict = {"items": {}, "coef_checks": []}

    for _ in range(30):
        resp = client.messages.create(
            model=MODEL, max_tokens=8000, system=EXTRACT_PROMPT, tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        if not uses:
            break
        results = []
        for u in uses:
            print(f"    - {u.name}({json.dumps(u.input, ensure_ascii=True)[:70]})")
            results.append({"type": "tool_result", "tool_use_id": u.id,
                            "content": _run_tool(onec, u.name, u.input, state)})
        messages.append({"role": "user", "content": results})

    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    start, end = raw.find("{"), raw.rfind("}")
    return json.loads(raw[start:end + 1])


def analyse_group(group: dict, items: list[NomItem], today: date) -> dict:
    """Детерминированная часть: изменения закупки/РРЦ, розница, пороги, сигналы."""
    new_p = Decimal(str(group["purchase"])) if group.get("purchase") is not None else None
    new_r = Decimal(str(group["rrc"])) if group.get("rrc") is not None else None

    rows = []
    for it in items:
        cur_p = Decimal(str(it.purchase.value)) if it.purchase else None
        cur_r = Decimal(str(it.rrc.value)) if it.rrc else None
        cur_ret = Decimal(str(it.retail.value)) if it.retail else None

        p_changed = new_p is not None and (cur_p is None or cur_p != new_p)
        r_changed = new_r is not None and (cur_r is None or cur_r != new_r)

        eff_p = new_p if new_p is not None else cur_p
        eff_r = new_r if new_r is not None else cur_r
        eff_r_date = today if r_changed else (
            date.fromisoformat(it.rrc.date) if it.rrc and it.rrc.date else None)

        dec = compute_retail(eff_p, rrc=eff_r, rrc_date=eff_r_date,
                             current_retail=cur_ret, today=today,
                             purchase_changed=p_changed)
        rows.append({"ref": it.ref, "name": it.name,
                     "cur_p": cur_p, "new_p": new_p if p_changed else None,
                     "cur_r": cur_r, "new_r": new_r if r_changed else None,
                     "cur_ret": cur_ret, "retail": dec})
    return {"group": group, "rows": rows}


def _transitions(rows: list[dict], label: str, cur_key: str, new_key: str) -> list[str]:
    """Строки «было → стало», сгруппированные по одинаковому переходу (как §10.3)."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        if r[new_key] is None:
            continue
        groups.setdefault((r[cur_key], r[new_key]), []).append(r)
    lines = []
    for (old, new), rs in groups.items():
        if old is None:
            lines.append(f"{label}: {_fmt(new)} — проставляется впервые, {len(rs)} поз.")
        else:
            lines.append(f"{label} {_fmt(old)} → {_fmt(new)} ({_pct(old, new)}), {len(rs)} поз.")
    return lines


def _retail_transitions(rows: list[dict]) -> list[str]:
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        if not r["retail"].write:
            continue
        groups.setdefault((r["cur_ret"], r["retail"].value), []).append(r)
    lines = []
    for (old, new), rs in groups.items():
        if old is None:
            lines.append(f"розница: {_fmt(new)} — проставляется впервые, {len(rs)} поз.")
        else:
            lines.append(f"розница {_fmt(old)} → {_fmt(new)} ({_pct(old, new)}), {len(rs)} поз.")
    return lines


def render(price_name: str, data: dict, analysed: list[dict], out: list[str]) -> None:
    """Сообщения агента в Telegram по одному прайсу."""
    add = out.append
    add(f"\n## Прайс: {price_name}\n")

    add("**Статусы** (одно редактируемое сообщение, §13.1 спеки):")
    add("```")
    add("Ищу прайсы в почте...")
    add("Определяю поставщика...")
    add("Проверяю выгрузку ТМ в 1С...")
    add("Определяю формат прайса...")
    add("Загружаю номенклатуру из 1С...")
    add("Сравниваю с другими поставщиками...")
    add("```")

    # --- вопрос 1: поставщик (справочник пуст — §4.5)
    add("\n### ❓ Вопрос 1 — идентификация поставщика (§4.5.5)\n")
    add("> Пришёл прайс «" + price_name + "»"
        + (f", в прайсе значится «{data.get('supplier_guess')}»." if data.get("supplier_guess") else ".")
        + " Этот отправитель мне не знаком.")
    add("> ")
    add("> Известных поставщиков пока нет — справочник пуст.")
    add("> ")
    add("> Это новый поставщик?")
    add("> • «новый» — заведу как «" + (data.get("supplier_guess") or price_name) + "» (имя можно задать своё)")
    add("> • назови другого — привяжу адрес к нему")
    add("\n**⏸ Ответ не даём — привязки не делаем, дальше прайс не обрабатывался бы.**")

    # --- вопрос 2: маппинг колонок
    cm = data.get("column_mapping") or {}
    if cm.get("ambiguous"):
        add("\n### ❓ Вопрос 2 — маппинг колонок (§6.5.5)\n")
        add(f"> {cm.get('question')}")
        add("\n**⏸ Ответ не даём — маппинг не сохраняем.** По спеке предложение ниже "
            "**не показывалось бы** до ответа: оно приведено только для проверки разбора, "
            f"в трактовке «закупка = {cm.get('purchase')}».")
    else:
        add("\n### Маппинг колонок определён без вопросов\n")
        add(f"- закупка: **{cm.get('purchase')}**, РРЦ: **{cm.get('rrc') or '—'}**, "
            f"база цены: **{cm.get('basis')}**")

    add("\n> Дальнейшее показано так, как выглядело бы **после** ответов на вопросы выше — "
        "чтобы был виден весь разбор. В 1С ничего не записано.\n")

    # --- бренды не в 1С
    absent = [b for b in data.get("brands", []) if not b.get("in_1c")]
    if absent:
        add("### Сообщение: бренды, которых нет в выгрузке на сайт (§8.1)\n")
        add("> По этим брендам цены не обновляю — их нет в списке ТМ к выгрузке:")
        for b in absent:
            add(f"> • {b['brand']}")

    # --- предложение по каждому бренду
    for block in analysed:
        b = block["brand"]
        tm_name = (b.get("tm_name") or "").strip()
        supplier = (data.get("supplier_guess") or "поставщик").split("(")[0].strip()
        add(f"\n### Предложение по ТМ «{tm_name}» (`{b['tm_code']}`)\n")
        add(f"> Обновляем цены от {supplier} на:")
        add(f"> — {tm_name}")
        for g in block["groups"]:
            gr = g["group"]
            lines = (_transitions(g["rows"], "закупка", "cur_p", "new_p")
                     + _transitions(g["rows"], "РРЦ", "cur_r", "new_r")
                     + _retail_transitions(g["rows"]))
            add(f">   • {gr['collection_1c']} — {len(g['rows'])} поз.")
            for line in lines or [">     цены совпадают — обновлять нечего"[6:]]:
                add(f">     {line}")
        add("> ?")

        # предупреждения
        warns: list[str] = []
        quiet: list[str] = []          # шумные пункты — одной сводной строкой
        n_unchanged = n_below_thr = 0
        for g in block["groups"]:
            gr = g["group"]
            below_p = [r for r in g["rows"] if r["retail"].warning == "rrc_below_purchase"]
            below_ret = [r for r in g["rows"] if r["retail"].warning == "rrc_below_retail"]
            n_below_thr += sum(1 for r in g["rows"] if r["retail"].reason == "below_threshold")
            unchanged = [r for r in g["rows"] if r["retail"].reason == "purchase_unchanged"]
            n_unchanged += len(unchanged)
            if unchanged:
                quiet.append(gr["collection_1c"])
            if below_p:
                r0 = below_p[0]
                warns.append(f"⚠️ {gr['collection_1c']}: **РРЦ ниже закупки** — проверьте прайс "
                             f"({len(below_p)} поз.: РРЦ {_fmt(r0['new_r'] or r0['cur_r'])} "
                             f"при закупке {_fmt(r0['new_p'] or r0['cur_p'])})")
            if below_ret:
                r0 = below_ret[0]
                warns.append(f"⚠️ {gr['collection_1c']}: **наша розница выше РРЦ** "
                             f"({len(below_ret)} поз.: розница {_fmt(r0['retail'].value)} "
                             f"при РРЦ {_fmt(r0['new_r'] or r0['cur_r'])}) — цену не меняю")
            if gr.get("confidence") == "disputed":
                warns.append(f"⚠️ сопоставление спорное: {gr['price_row']} → "
                             f"{gr['collection_1c']} — {gr.get('note')}")
        if n_unchanged:
            warns.append(f"ℹ️ закупка не меняется у {n_unchanged} поз. "
                         f"({len(quiet)} колл.) — розница не пересчитывалась")
        if n_below_thr:
            warns.append(f"ℹ️ розница не пишется у {n_below_thr} поз. — изменение меньше 2%")
        for u in b.get("unmatched", []):
            warns.append(f"ℹ️ не сопоставлено: {u['price_row']} — {u['reason']}")
        warns.append("ℹ️ других поставщиков этой ТМ в истории нет — сравнивать не с чем (§9.4)")
        if warns:
            add("\n**Предупреждения к предложению (§9.3):**\n")
            for w in warns:
                add(f"> {w}")

        add("\n**⏸ Финальный вопрос без ответа — `set-prices` НЕ вызывается.**")

    for w in data.get("warnings", []):
        add(f"\n> ℹ️ {w}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="подстрока имени файла прайса")
    args = ap.parse_args()

    cfg = load_config()
    onec = OnecClient(cfg.onec_base_url, cfg.onec_token, timeout=120)
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    today = date.today()

    files = [f for f in sorted(glob.glob(os.path.join(PRICE_DIR, "*")))
             if os.path.isfile(f) and f.lower().endswith((".xlsx", ".xls", ".csv", ".pdf"))]
    if args.only:
        files = [f for f in files if args.only.lower() in os.path.basename(f).lower()]

    out = [f"# Сообщения агента админу в Telegram\n",
           f"_Прогон {today:%d.%m.%Y}: три прайса из `.claude/test-prices`, живая 1С (только чтение)._\n",
           "Разбор и сопоставление — модель; цены, розница, пороги и предупреждения — "
           "детерминированный расчёт по `retail-price-rules.md`.",
           "**Вопросы оставлены без ответа: ни одной записи в 1С не сделано.**\n",
           "---"]

    nom_cache: dict[str, list[NomItem]] = {}
    for path in files:
        name = os.path.basename(path)
        print(f"\n=== {name}")
        sheet = next((v for k, v in SHEETS.items() if k.lower() in name.lower()), None)
        text, images = price_to_text(open(path, "rb").read(), name, sheet=sheet)
        data = extract(client, onec, text, images,
                       f"Файл прайса: {name}" + (f" (лист: {sheet})" if sheet else ""))

        analysed = []
        for b in data.get("brands", []):
            if not b.get("in_1c") or not b.get("tm_code"):
                continue
            if b["tm_code"] not in nom_cache:
                nom_cache[b["tm_code"]] = onec.by_tm_all(b["tm_code"])
            items = nom_cache[b["tm_code"]]
            groups, seen = [], {}
            for g in b.get("groups", []):
                ref = g.get("collection_ref")
                sel = [i for i in items if i.collection_ref == ref]
                if not sel:
                    continue
                key = (ref, g.get("purchase"), g.get("rrc"))
                if key in seen:                     # та же коллекция и те же цены — дубль
                    continue
                if ref in [k[0] for k in seen]:     # та же коллекция, но цены другие
                    g = dict(g, confidence="disputed",
                             note=(g.get("note") or "") +
                             " | ВНИМАНИЕ: на эту же коллекцию 1С претендует другая строка "
                             "прайса с другими ценами")
                seen[key] = True
                groups.append(analyse_group(g, sel, today))
            if groups:
                analysed.append({"brand": b, "groups": groups})

        render(name, data, analysed, out)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"\n--- {OUT}")
    onec.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
