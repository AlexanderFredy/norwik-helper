"""Обход «у кого из новых товаров нет фото»: 1С → сайт → строки отчёта.

Вынесено из инструмента агента, потому что зовущих теперь двое: вопрос менеджера («покажи,
где не добавлены фото») и ежедневная проверка, наполняющая журнал (`storage/photo_watch`).
Два прохода, разошедшиеся в мелочи — скажем, в том, считать ли снятые, — дали бы два разных
ответа на один вопрос, и разница всплыла бы не сразу.

Клиент 1С передаётся параметром, а не импортируется: модуль про сайт, и знать про устройство
1С ему незачем — довольно того, что у объекта есть `selling_tm` и `by_tm_all`.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from src.model import normalize as nz
from src.website_tool import photo_report, photos


@dataclass
class Scan:
    """Что видно после обхода."""
    rows: list = field(default_factory=list)          # позиции без фото, photo_report.Row
    observations: list = field(default_factory=list)  # для журнала наблюдений
    checked: int = 0            # сколько новых позиций проверено
    marks: int = 0              # сколько марок в области поиска
    no_card: int = 0            # карточки на сайте ещё нет
    failed: int = 0             # сайт не ответил
    lost: int = 0               # 1С не отдала позицию
    problem: str = ""           # результату верить нельзя — и вот почему
    unknown_tms: list = field(default_factory=list)   # названные марки, которых нет
    note: str = ""              # оговорка к результату (обход по маркам вместо запроса)


async def scan(onec, *, since: str, tm: str = "", extra: list | None = None) -> Scan:
    """Обойти новые товары и узнать, у кого нет фото.

    `since` — ГГГГ-ММ-ДД, граница «новизны». `tm` сужает до одной марки.
    `extra` — позиции из журнала, которые надо перепроверить, даже если 1С больше не
    считает их новыми: иначе товар, заведённый давно и до сих пор без фото, пропал бы из
    виду ровно тогда, когда стал самым просроченным. Это словари с ref, site_id, tm,
    collection, name, created.
    """
    out = Scan()

    marks = await asyncio.to_thread(onec.selling_tm)
    if tm:
        marks = [m for m in marks if tm.lower() in m.name.lower()]
        if not marks:
            out.problem = f"Марки «{tm}» нет среди выгружаемых на сайт."
            return out
        walk = [(m.code, m.name) for m in marks]
    else:
        # ОДИН ЗАПРОС ВМЕСТО СТА ШЕСТИДЕСЯТИ ШЕСТИ (замер 30.09.2026: 285 с против 54 с).
        walk = [(None, "")]
    out.marks = len(marks)

    fresh: list[tuple[str, object]] = []
    dated = False
    for code, label in walk:
        nom = await asyncio.to_thread(onec.by_tm_all, code, created_from=since)
        if any(str(e.get("code")) == "tm_missing" for e in nom.errors):
            out.problem = ("1С не принимает запрос без марки — нужна выкладка "
                           "обновлённого specs/1c/by-tm.bsl.")
            return out
        out.lost += len(nom.errors)
        dated = dated or any(i.created for i in nom.items)
        for item in nom.items:
            # Отбор ПОВТОРЯЕТСЯ здесь, а не доверяется 1С: со старым by-tm.bsl параметр
            # `created_from` проходит мимо, и выгрузка приходит целиком.
            if not item.not_exported and item.created and item.created >= since:
                fresh.append((label or item.tm or "Без марки", item))
        if nom.items and not dated:
            out.problem = ("1С не отдаёт дату создания товаров — отобрать новые нечем. "
                           "Нужна выкладка обновлённого specs/1c/by-tm.bsl.")
            return out

    # Позиции из журнала, которых уже нет в окне новизны. Дубли отсекаем по коду 1С:
    # свежая выгрузка точнее журнала, там имя и коллекция сегодняшние.
    seen = {i.ref for _, i in fresh}
    watched = [w for w in (extra or []) if w.get("ref") not in seen]

    ids = [i.id for _, i in fresh] + [w.get("site_id", "") for w in watched]
    checker = photos.PhotoChecker()
    try:
        state = await asyncio.to_thread(checker.statuses, ids)
    finally:
        checker.close()

    def note(ref, site_id, tm_name, collection, name, created, verdict):
        out.observations.append({
            "ref": ref, "site_id": site_id, "tm": tm_name, "collection": collection,
            "name": name, "created": created, "state": verdict})
        if verdict == photos.NONE:
            out.rows.append(photo_report.Row(
                tm=tm_name, collection=collection, name=name,
                url=photos.item_url(site_id), created=created))
        elif verdict == photos.NO_CARD:
            out.no_card += 1
        elif verdict == photos.FAILED:
            out.failed += 1

    for tm_name, item in fresh:
        note(item.ref, item.id, tm_name, nz.collection_of(item),
             item.site_name or item.name, item.created,
             state.get((item.id or "").strip(), photos.NO_CARD))

    for w in watched:
        site_id = (w.get("site_id") or "").strip()
        note(w.get("ref", ""), site_id, w.get("tm", ""), w.get("collection", ""),
             w.get("name", ""), w.get("created", ""),
             state.get(site_id, photos.NO_CARD))

    out.checked = len(fresh) + len(watched)
    return out


def _half_keys(name: str) -> set[str]:
    key = " ".join(str(name or "").casefold().replace("ё", "е").split())
    return {key} | {" ".join(p.split()) for p in key.split("/") if p.strip()}


def match_marks(wanted: list[str], marks: list) -> tuple[list, list[str]]:
    """Марки по названиям из вопроса. Возвращает (найденные марки, ненайденные имена).

    Половина двуязычного имени целиком главнее вхождения: «Atlas Concorde» — это «Atlas
    Concorde / Атлас конкорд», а не заодно «Atlas Concorde Rus». Ненайденное имя не
    отбрасывается молча — его называет ответ.
    """
    picked, unknown = [], []
    for raw in wanted:
        key = " ".join(str(raw or "").casefold().replace("ё", "е").split())
        if not key:
            continue
        exact = [m for m in marks if key in _half_keys(m.name)]
        loose = exact or [m for m in marks if any(key in k for k in _half_keys(m.name))]
        if not loose:
            unknown.append(raw)
        for m in loose:
            if m not in picked:
                picked.append(m)
    return picked, unknown


async def scan_category(onec, *, type_code: str, type_name: str = "",
                        tms: list[str] | None = None, since: str = "") -> Scan:
    """Товары КАТЕГОРИИ без фото — только выгружаемые на сайт (решение админа 11.10.2026).

    Категория обязательна, и это не вежливость: обход всего каталога запрещён — он стоил
    бы минут на каждый вопрос. Марки (`tms`) сужают ещё, по желанию.

    Выгружаемыми считаются позиции без «Не выгружать» и с id сайта; без id товар на сайт не
    выгружался, и «фото нет» про него — не новость. Марки — только помеченные к выгрузке
    (`selling_tm`). `since` (ГГГГ-ММ-ДД) — сузить до заведённых с даты, если спросили про
    новые.

    1С: запрос без марки с ограничителем по виду товара — один на категорию. Старый
    by-tm.bsl такого не принимает (`tm_missing`) — тогда обход помеченных марок с тем же
    фильтром, медленнее, и об этом говорит `note`.
    """
    out = Scan()
    # Без категории — только недавно заведённые: каталог целиком не обходим НИКОГДА, и
    # ограничить выборку может лишь одно из двух (решение админа 12.10.2026).
    if not type_code and not since:
        out.problem = ("Назови категорию товара — анализ по всему каталогу не делаю.")
        return out
    marks = await asyncio.to_thread(onec.selling_tm)
    if tms:
        marks, out.unknown_tms = match_marks(list(tms), marks)
        if not marks:
            out.problem = ("Среди выгружаемых на сайт нет марок: "
                           + ", ".join(f"«{t}»" for t in out.unknown_tms) + ".")
            return out
    out.marks = len(marks)
    names = {m.code: m.name for m in marks}

    walk = [(m.code, m.name) for m in marks] if tms else [(None, "")]
    picked: list[tuple[str, object]] = []
    kw = {"product_type": type_code} if type_code else {}
    if since:
        kw["created_from"] = since

    index = 0
    while index < len(walk):
        code, label = walk[index]
        index += 1
        nom = await asyncio.to_thread(onec.by_tm_all, code, **kw)
        if code is None and any(str(e.get("code")) == "tm_missing" for e in nom.errors):
            # Старый BSL: без марки нельзя. Обходим помеченные марки — тот же ответ, дольше.
            walk = [(m.code, m.name) for m in marks]
            index = 0
            out.note = ("1С ещё не принимает запрос по виду товара без марки — обошёл марки "
                        "по одной; ускорится после выкладки specs/1c/by-tm.bsl.")
            continue
        out.lost += sum(1 for e in nom.errors if e.get("ref"))
        for item in nom.items:
            # Отбор ПОВТОРЯЕТСЯ в Python: старый BSL фильтры мог и не применить.
            if item.not_exported or not (item.id or "").strip():
                continue
            if type_code and (item.product_type_ref or "").strip() != type_code:
                continue
            if since and not (item.created and item.created >= since):
                continue
            tm_name = label or item.tm or names.get(getattr(item, "tm_code", ""), "")
            if not tms or tm_name in names.values():
                picked.append((tm_name or "Без марки", item))

    checker = photos.PhotoChecker()
    try:
        state = await asyncio.to_thread(checker.statuses, [i.id for _, i in picked])
    finally:
        checker.close()

    for tm_name, item in picked:
        verdict = state.get(item.id.strip(), photos.NO_CARD)
        out.observations.append({
            "ref": item.ref, "site_id": item.id, "tm": tm_name,
            "collection": nz.collection_of(item), "name": item.site_name or item.name,
            "created": item.created, "state": verdict})
        if verdict == photos.NONE:
            out.rows.append(photo_report.Row(
                tm=tm_name, collection=nz.collection_of(item),
                name=item.site_name or item.name, url=photos.item_url(item.id),
                created=item.created))
        elif verdict == photos.NO_CARD:
            out.no_card += 1
        elif verdict == photos.FAILED:
            out.failed += 1
    out.checked = len(picked)
    return out
