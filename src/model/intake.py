"""Приём прайса (§4 specs/agent-workflow-model.md).

Один провайдеронезависимый метод с признаком «принудительно». Обычный приём проверяет
свежесть и отклоняет устаревший, принудительный проверку пропускает.

**«Принудительно» — признак ПОСТУПЛЕНИЯ, а не спасение уже отклонённого.** Чтобы понять,
что прайс устарел, файл надо прочитать, и после отказа он удаляется — спасать было бы уже
нечего. Поэтому признак известен ДО проверки свежести.

**Задачи здесь пока ЗАГЛУШКА.** Настоящий список заводит LLM по результатам разбора (§6.1);
до тех пор `stub_tasks` строит по пять задач на каждый лист файла, чтобы можно было
проверить работу со списком: сортировку, статусы, захват, пересборку. Заглушка помечена в
описании каждой задачи — увидев её в боевом прогоне, не спутаешь с настоящей.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from src.model.enums import TaskKind
from src.model.price import Price, SupplierPrice
from src.model.refs import Ref, TaskAddress
from src.model.task import PriceTask
from src.price_tool.freshness import date_from_name, now_stamp
from src.price_tool.parser import parse_price_table
from src.price_tool.signature import price_signature

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Intake:
    """Что вышло из приёма. `price is None` — прайс не принят, и в `reason` сказано почему."""
    price: Price | None
    reason: str = ""
    supplier_name: str = ""
    outdated: bool = False


def read_signature(content: bytes, filename: str) -> tuple[str, list]:
    """Сигнатура формата и разобранные листы. Неразобранный файл — не повод падать."""
    try:
        sheets = parse_price_table(content, filename)
    except Exception:                                   # noqa: BLE001
        logger.warning("Не удалось разобрать %s", filename, exc_info=True)
        return "", []
    return (price_signature(sheets) if sheets else ""), list(sheets or [])


def stub_tasks(sheets, tm_name: str) -> list[PriceTask]:
    """ЗАГЛУШКА вместо задач от LLM.

    По пять задач (все виды) на каждый лист файла: так список получается похожим на
    настоящий по размеру и порядку, и на нём видно сортировку, статусы и пересборку.
    """
    names = [s.name for s in sheets if getattr(s, "name", "")] or ["весь прайс"]
    out: list[PriceTask] = []
    for sheet in names:
        address = TaskAddress(tm=Ref.make(names=[tm_name]),
                              subject=Ref.make(names=[sheet]))
        for kind in TaskKind:
            out.append(PriceTask(
                kind=kind, address=address,
                description=f"ЗАГЛУШКА: {kind.value} по разделу «{sheet}». "
                            "Настоящие задачи заведёт агент, в 1С ничего не пишется."))
    return out


async def submit(content: bytes, filename: str, *, suppliers, model_store, prices,
                 supplier_hint: str = "", force: bool = False,
                 received_at: str | None = None, price_date: str | None = None,
                 save_file) -> Intake:
    """Принять файл прайса.

    `prices` — текущий список модели (для проверки свежести), `save_file(content, filename)`
    кладёт файл на диск и возвращает путь. Файл сохраняется ДО проверки свежести, потому что
    без него не посчитать сигнатуру, — и удаляется, если прайс отклонён.
    """
    from src.model import price_list as pl

    signature, sheets = read_signature(content, filename)

    # Даты берём здесь, иначе свежесть считать не по чему: `is_newer` смотрит сперва дату
    # САМОГО прайса (её несёт имя файла), потом дату получения. Без них два прайса одного
    # поставщика выглядели бы одинаково свежими, и «есть более новый» не сработало бы
    # никогда — ни в тесте, ни в бою.
    price_date = price_date or date_from_name(filename)
    received_at = received_at or now_stamp()

    # Личность формата считаем ОДИН РАЗ и до опознания: по ней ищется владелец, ею же
    # заводится запись. Разойдись эти две величины — поиск смотрел бы на одно, а
    # закреплялось бы другое, и справочник не узнавал бы собственные записи.
    # Файл, который не разобрался, тоже имеет личность — хеш содержимого. По имени её
    # строить нельзя: тот же файл под другим именем стал бы «другим форматом».
    sig_hash = signature or "без-разбора:" + hashlib.sha1(content).hexdigest()[:16]

    # --- поставщик: имя админа главнее всего (§2.2)
    name = (supplier_hint or "").strip()
    if not name:
        # Сигнатуру уже видели — берём её владельца. Несколько кандидатов разрешает LLM;
        # пока её здесь нет, берём первого и пишем в лог, что выбор был неоднозначным.
        owners = await suppliers.find_signatures(sig_hash)
        if owners:
            if len(owners) > 1:
                logger.info("Сигнатура %s есть у %d поставщиков — взят первый",
                            sig_hash[:12], len(owners))
            found = await suppliers.get_supplier(owners[0].supplier_id)
            name = found.name if found else ""
    if not name:
        name = (filename.rsplit(".", 1)[0] or "Без названия").strip()

    supplier = await suppliers.add_supplier(name)
    sig = await suppliers.add_signature(supplier.id, sig_hash, sample_name=filename)

    path = save_file(content, filename)
    if not path:
        return Intake(None, "не удалось сохранить файл на сервере", supplier.name)

    record = await suppliers.add_price_file(sig.id, filename, str(path),
                                            received_at=received_at)

    candidate = Price(supplier_price=SupplierPrice(
        supplier_id=supplier.id, file_id=record.id, file_path=str(path),
        filename=filename, signature=sig.signature, received_at=received_at,
        price_date=price_date))

    outdated = pl.is_outdated(candidate, list(prices) + [candidate])

    if outdated and not force:
        # Отклонён: от него не остаётся ничего — ни файла, ни записи о нём. Поставщик и
        # сигнатура сохраняются: они про опознание, а не про прайс (§4).
        #
        # НО СНАЧАЛА ПРОВЕРЯЕМ, не ссылается ли на этот файл уже принятый прайс. Файлы
        # именуются по содержимому, поэтому повторно присланный тот же прайс даёт ТОТ ЖЕ
        # путь — и наивное удаление снесло бы запись, принадлежащую живому прайсу.
        busy = any(p.supplier_price.file_path == str(path) for p in prices)
        if not busy:
            await suppliers.delete_price_file(record.id)
            _drop(path)
        return Intake(None, "есть более свежий прайс этого поставщика того же формата",
                      supplier.name, outdated=True)

    for task in stub_tasks(sheets, supplier.name):
        candidate.add_task(task)

    await model_store.add_price(candidate)
    return Intake(candidate, "", supplier.name, outdated=outdated)


def _drop(path) -> None:
    from pathlib import Path
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:                                     # noqa: BLE001
        logger.warning("Не удалось убрать отклонённый прайс %s", path, exc_info=True)
