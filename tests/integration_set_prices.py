"""Боевая проверка записи цен в 1С: POST get-products/set-prices (specs/content-manager.md §10).

ЗАПИСЫВАЕТ ЦЕНЫ В БОЕВУЮ 1С. Перед записью снимает «до» в snapshot-файл — откат делается
повторной записью старых значений (регистр периодичности ДЕНЬ: запись за сегодня
перезаписывается, история не растёт).

Что меняем — расхождения из отчётов сопоставления (.claude/test-prices/reports):
  · Most Flooring / Excellent  — РРЦ 2270 → 2070   форма (а), вся коллекция
  · Most Flooring / Brilliant  — РРЦ 1780 → 1960   форма (а), вся коллекция
  · Peli / Vintage             — закупка 949 → 999 форма (б), точечно по 5 товарам

Коды папок для формы (а) берутся из by-tm (`parent.code`) — руками задавать не нужно.

Запуск:
    .venv\\Scripts\\python -m tests.integration_set_prices --dry        # показать payload
    .venv\\Scripts\\python -m tests.integration_set_prices --probe      # только пути ошибок
    .venv\\Scripts\\python -m tests.integration_set_prices              # запись + сверка
    .venv\\Scripts\\python -m tests.integration_set_prices --revert     # вернуть как было
    .venv\\Scripts\\python -m tests.integration_set_prices --collections YO-xxxx YO-yyyy
                                                       # форма (а): цены на всю коллекцию

Отчёт — tests/out/set_prices_report.md, снимок «до» — tests/out/before_prices.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

import httpx

from src.config import load_config
from src.onec.client import OnecClient, NomItem

OUT_DIR = os.path.join(os.path.dirname(__file__), "out")
SNAPSHOT = os.path.join(OUT_DIR, "before_prices.json")
REPORT = os.path.join(OUT_DIR, "set_prices_report.md")

TM_MOST = "000000311"   # Most Flooring
TM_PELI = "000000298"   # Peli

EXCELLENT = ["YO-00075141", "YO-00075142", "YO-00075143", "YO-00075144",
             "YO-00075145", "YO-00075146", "YO-00075147", "YO-00077618"]
BRILLIANT = ["YO-00070616", "YO-00070618", "YO-00070620", "YO-00070621",
             "YO-00070622", "YO-00070623", "YO-00077627", "YO-00077628"]
VINTAGE = ["YO-00068864", "YO-00068865", "YO-00068866", "YO-00068867", "YO-00068869"]

# (ref, ТМ, вид цены, новое значение) — ожидаемый результат для сверки
PLAN = ([(r, TM_MOST, "rrc", 2070.0) for r in EXCELLENT]
        + [(r, TM_MOST, "rrc", 1960.0) for r in BRILLIANT]
        + [(r, TM_PELI, "purchase", 999.0) for r in VINTAGE])

# Две коллекции — формой (а) по collection_ref (код папки берётся из by-tm по имени),
# точечные товары — формой (б) по ref.
COLLECTION_BATCHES = [
    ("1. Most Flooring / Excellent — РРЦ 2270 → 2070, вся коллекция",
     TM_MOST, "Excellent", "rrc", 2070.0),
    ("2. Most Flooring / Brilliant — РРЦ 1780 → 1960, вся коллекция",
     TM_MOST, "Brilliant", "rrc", 1960.0),
]
ITEM_BATCHES = [
    ("3. Peli / Vintage — закупка 949 → 999, точечно по товарам",
     VINTAGE, TM_PELI, "purchase", 999.0),
]

ERROR_PROBES = [
    ("нет prices", {"tm": TM_MOST}),
    ("нет tm", {"ref": "YO-00075141", "prices": {"rrc": 2070}}),
    ("несуществующая ТМ", {"tm": "000000999", "ref": "YO-00075141", "prices": {"rrc": 2070}}),
    ("товар чужой ТМ (Peli под Most Flooring)",
     {"tm": TM_MOST, "ref": "YO-00068864", "prices": {"purchase": 999}}),
    ("несуществующий товар", {"tm": TM_MOST, "ref": "YO-99999999", "prices": {"rrc": 2070}}),
    ("несуществующая папка-коллекция",
     {"tm": TM_MOST, "collection_ref": "YO-99999999", "prices": {"rrc": 2070}}),
    ("collection_ref указывает на товар, а не на папку",
     {"tm": TM_MOST, "collection_ref": "YO-00075141", "prices": {"rrc": 2070}}),
    ("нулевая цена", {"tm": TM_MOST, "ref": "YO-00075141", "prices": {"rrc": 0}}),
    ("цена строкой", {"tm": TM_MOST, "ref": "YO-00075141", "prices": {"rrc": "2070"}}),
    ("ни ref, ни collection_ref", {"tm": TM_MOST, "prices": {"rrc": 2070}}),
]

_lines: list[str] = []


def log(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:      # консоль cp1251 — не роняем прогон
        print(text.encode("ascii", "replace").decode())
    _lines.append(text)


def post_set_prices(base_url: str, token: str, payload: dict, tries: int = 4):
    """→ (http_code, распарсенный json | None, текст проблемы | None)."""
    url = base_url.rstrip("/") + "/get-products/set-prices"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last: Exception | None = None
    for attempt in range(tries):
        try:
            r = httpx.post(url, content=body, timeout=300,
                           headers={"X-API-Token": token,
                                    "Content-Type": "application/json"})
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
            continue
        text = r.content.decode("utf-8-sig", errors="replace")
        if "<!DOCTYPE" in text:
            return r.status_code, None, "1С отдал HTML вместо JSON (исключение мимо обработчика)"
        try:
            return r.status_code, json.loads(text), None
        except json.JSONDecodeError:
            return r.status_code, None, text[:400]
    return 0, None, f"нет связи с 1С: {last}"


def price(item: NomItem, kind: str):
    return item.rrc if kind == "rrc" else item.purchase


def read_all(client: OnecClient) -> dict[str, NomItem]:
    out: dict[str, NomItem] = {}
    for tm in (TM_MOST, TM_PELI):
        for it in client.by_tm_all(tm).items:
            out[it.ref] = it
    return out


def collection_code(items: dict[str, NomItem], tm_items: list[str], name: str) -> str | None:
    """Код папки-коллекции из by-tm (parent.code). None — сервис ещё отдаёт parent строкой."""
    for ref in tm_items:
        it = items.get(ref)
        if it and it.collection == name and it.collection_ref:
            return it.collection_ref
    return None


def save_snapshot(items: dict[str, NomItem]) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    data = {ref: {"name": it.name, "collection": it.collection,
                  "purchase": it.purchase.value if it.purchase else None,
                  "purchase_date": it.purchase.date if it.purchase else None,
                  "rrc": it.rrc.value if it.rrc else None,
                  "rrc_date": it.rrc.date if it.rrc else None}
            for ref, it in items.items()}
    with open(SNAPSHOT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def load_snapshot() -> dict:
    with open(SNAPSHOT, encoding="utf-8") as f:
        return json.load(f)


def report_response(data: dict) -> None:
    log("```json")
    log(json.dumps(data, ensure_ascii=False, indent=1))
    log("```")


def _fmt(value) -> str:
    return "нет" if value is None else f"{value:g}"


def telegram_preview(responses: list[dict]) -> None:
    """Сообщение админу, собранное ТОЛЬКО из ответов — без повторных запросов в 1С (§13.1)."""
    log("\n## Сообщение админу (собрано только из ответов set-prices)\n")
    kinds = {"purchase": "закупка", "rrc": "РРЦ"}
    total_upd = total_unch = total_fail = 0
    day = ""
    lines: list[str] = []

    for data in responses:
        day = data.get("date", day)
        total_upd += data.get("updated", 0)
        total_unch += data.get("unchanged", 0)
        total_fail += data.get("failed", 0)
        for res in data.get("results", []):
            head = f"{res.get('tm_name', '')} / {res.get('collection', '')}"
            if "changes" in res:                       # форма (а)
                lines.append(f"• **{head}** — {res.get('count')} поз.")
                for ch in res["changes"]:
                    mark = " (уже актуально)" if ch.get("skipped") else ""
                    lines.append(
                        f"    ↳ {kinds.get(ch['price_type'], ch['price_type'])}: "
                        f"{_fmt(ch.get('old'))} → {_fmt(ch.get('new'))}"
                        f" — {ch.get('count')} поз.{mark}")
            else:                                      # форма (б)
                for kind, det in (res.get("written") or {}).items():
                    lines.append(
                        f"• **{head}** · {res.get('name', '')} — "
                        f"{kinds.get(kind, kind)}: {_fmt(det.get('old'))} → {_fmt(det.get('new'))}")
                # позиции, где цена уже актуальна, тоже должны быть видны админу
                for kind, det in (res.get("skipped") or {}).items():
                    lines.append(
                        f"• **{head}** · {res.get('name', '')} — "
                        f"{kinds.get(kind, kind)}: {_fmt(det.get('value'))} (уже актуально)")
        for err in data.get("errors", []):
            lines.append(f"• ⚠️ {err.get('code')}: {err.get('message')}")

    log("> **Цены обновлены**" + (f" {day}" if day else "")
        + f" — {total_upd} позиций"
        + (f", без изменений {total_unch}" if total_unch else "")
        + (f", ошибок {total_fail}" if total_fail else ""))
    for line in lines:
        log("> " + line)


def run_probes(base_url: str, token: str) -> None:
    log("\n## Пути ошибок (записей быть не должно)\n")
    log("| случай | ответ 1С |")
    log("|---|---|")
    for name, item in ERROR_PROBES:
        code, data, err = post_set_prices(base_url, token, {"items": [item]})
        if err:
            cell = f"HTTP {code} — {err}"
        else:
            cell = f"HTTP {code} `{json.dumps(data, ensure_ascii=False)}`"
        log(f"| {name} | {cell} |")


def verify(client: OnecClient, before: dict, expected: list[tuple[str, str, float]]) -> None:
    now = read_all(client)
    log("\n## Сверка после записи\n")
    log("| ref | наименование | вид цены | было | стало | дата | ок |")
    log("|---|---|---|---|---|---|---|")
    ok = bad = 0
    for ref, kind, want in expected:
        was = (before.get(ref) or {}).get(kind)
        item = now.get(ref)
        got = price(item, kind) if item else None
        good = got is not None and abs(got.value - want) < 0.005
        ok, bad = (ok + 1, bad) if good else (ok, bad + 1)
        log("| {} | {} | {} | {} | {} | {} | {} |".format(
            ref, item.name if item else "?", kind,
            was if was is not None else "—",
            got.value if got else "—", got.date if got else "—",
            "OK" if good else "ОШИБКА"))
    log(f"\nСовпало: **{ok}**, расхождений: **{bad}**.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="показать payload, ничего не писать")
    ap.add_argument("--probe", action="store_true", help="только пути ошибок")
    ap.add_argument("--revert", action="store_true", help="вернуть цены из снимка «до»")
    ap.add_argument("--collections", nargs="*", default=[],
                    help="переопределить коды папок для формы (а): <Excellent> <Brilliant>; "
                         "по умолчанию берутся из by-tm (parent.code)")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.onec_base_url or not cfg.onec_token:
        print("Нужны ONEC_BASE_URL и ONEC_TOKEN в .env")
        return 2

    os.makedirs(OUT_DIR, exist_ok=True)
    client = OnecClient(cfg.onec_base_url, cfg.onec_token, timeout=120)
    log(f"# Прогон set-prices — {datetime.now():%Y-%m-%d %H:%M}\n")

    try:
        if args.dry:
            for title, tm, name, kind, value in COLLECTION_BATCHES:
                log(f"\n## {title}\n")
                report_response({"items": [{"tm": tm, "collection_ref": f"<код папки {name}>",
                                            "prices": {kind: value}}]})
            for title, refs, tm, kind, value in ITEM_BATCHES:
                log(f"\n## {title}\n")
                report_response({"items": [{"ref": r, "tm": tm, "prices": {kind: value}}
                                           for r in refs]})
            return 0

        if args.revert:
            before = load_snapshot()
            items, expected = [], []
            for ref, tm, kind, _ in PLAN:
                old = (before.get(ref) or {}).get(kind)
                if old is None:
                    log(f"> ⚠️ {ref}: {kind} до прогона не было — вернуть нельзя "
                        f"(set-prices не удаляет записи регистра)")
                    continue
                items.append({"ref": ref, "tm": tm, "prices": {kind: old}})
                expected.append((ref, kind, old))
            log(f"\n## Откат: {len(items)} позиций\n")
            code, data, err = post_set_prices(cfg.onec_base_url, cfg.onec_token, {"items": items})
            log(f"HTTP {code}" + (f" — {err}" if err else ""))
            if data:
                report_response(data)
            verify(client, before, expected)
            return 0

        # снимок «до» — до любой записи
        before_items = read_all(client)
        save_snapshot(before_items)
        before = load_snapshot()
        log(f"Снимок «до»: {len(before)} позиций → `{os.path.relpath(SNAPSHOT)}`")

        run_probes(cfg.onec_base_url, cfg.onec_token)
        if args.probe:
            return 0

        responses: list[dict] = []
        forced = list(args.collections)
        for idx, (title, tm, name, kind, value) in enumerate(COLLECTION_BATCHES):
            log(f"\n## {title}\n")
            refs = EXCELLENT if name == "Excellent" else BRILLIANT
            folder = forced[idx] if idx < len(forced) else collection_code(before_items, refs, name)
            if not folder:
                log(f"⚠️ Пропуск: by-tm не отдал код папки для «{name}» "
                    f"(parent должен быть объектом {{code, name}})")
                continue
            log(f"collection_ref = `{folder}`")
            payload = {"items": [{"tm": tm, "collection_ref": folder, "prices": {kind: value}}]}
            code, data, err = post_set_prices(cfg.onec_base_url, cfg.onec_token, payload)
            log(f"HTTP {code}" + (f" — {err}" if err else ""))
            if data:
                report_response(data)
                responses.append(data)
                if "changes" not in (data.get("results") or [{}])[0]:
                    log("⚠️ Ответ формы (а) без `changes` — в 1С старая версия обработчика")

        for title, refs, tm, kind, value in ITEM_BATCHES:
            log(f"\n## {title}\n")
            payload = {"items": [{"ref": r, "tm": tm, "prices": {kind: value}} for r in refs]}
            log(f"Позиций в запросе: {len(payload['items'])}")
            code, data, err = post_set_prices(cfg.onec_base_url, cfg.onec_token, payload)
            log(f"HTTP {code}" + (f" — {err}" if err else ""))
            if data:
                report_response(data)
                responses.append(data)

        telegram_preview(responses)
        verify(client, before, [(r, k, v) for r, _, k, v in PLAN])
        return 0
    finally:
        client.close()
        with open(REPORT, "w", encoding="utf-8") as f:
            f.write("\n".join(_lines))
        print(f"\n--- отчёт: {os.path.relpath(REPORT)} ---")


if __name__ == "__main__":
    sys.exit(main())
