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
