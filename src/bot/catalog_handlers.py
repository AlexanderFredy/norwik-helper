"""Команды справочников поставщиков, сигнатур и файлов (§2.3 spec/agent-workflow-model.md).

Отдельный роутер, а не добавка в `pricing_handlers`: там уже полторы тысячи строк про разбор
прайсов, и справочники к нему отношения не имеют.

**Номера сквозные по всему справочнику.** Админ набирает номер из списка, и если бы
нумерация зависела от фильтра, `/signature_delete 2` удалял бы разное в разных видах. Поэтому
номер — позиция в ПОЛНОМ списке, а фильтр только прячет лишние строки.
"""
import asyncio
import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from src.model import collection_audit as audit
from src.model.events import Event, EventKind
from src.price_tool import catalog_view as view
from src.website_tool import photo_report as audit_photos
from src.storage.suppliers import SupplierStore

logger = logging.getLogger(__name__)

router = Router()


def _pick(items, arg: str):
    """Элемент по номеру из списка. None — номер не подошёл."""
    if arg.isdigit() and 1 <= int(arg) <= len(items):
        return items[int(arg) - 1]
    return None


async def _deny(message: Message) -> None:
    await message.answer("Команда доступна только администратору.")


# ------------------------------------------------------------------ поставщики

@router.message(Command("suppliers"))
async def cmd_suppliers(message: Message, supplier_store: SupplierStore,
                        is_admin: bool) -> None:
    if not is_admin:
        return await _deny(message)
    rows = await supplier_store.list_suppliers()
    counts = await supplier_store.supplier_counts()
    tail = ("\n\nПравка: /supplier_add, /supplier_rename, /supplier_delete, /supplier_merge"
            if rows else "")
    await message.answer(view.render_suppliers(rows, counts) + tail)


@router.message(Command("supplier_add"))
async def cmd_supplier_add(message: Message, command: CommandObject,
                           supplier_store: SupplierStore, is_admin: bool) -> None:
    if not is_admin:
        return await _deny(message)
    name = (command.args or "").strip()
    if not name:
        await message.answer("Укажите имя: /supplier_add Монарх Логистик")
        return

    before = await supplier_store.find_supplier(name)
    made = await supplier_store.add_supplier(name)
    if before is not None:
        await message.answer(f"Такой поставщик уже есть: «{made.name}». "
                             "Имена сравниваются без учёта регистра и пунктуации.")
        return
    await message.answer(f"Поставщик «{made.name}» заведён. Список: /suppliers")


@router.message(Command("supplier_rename"))
async def cmd_supplier_rename(message: Message, command: CommandObject,
                              supplier_store: SupplierStore, is_admin: bool,
                              model=None) -> None:
    if not is_admin:
        return await _deny(message)
    parts = (command.args or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Нужен номер из /suppliers и новое имя: "
                             "/supplier_rename 2 Монарх Логистик")
        return

    rows = await supplier_store.list_suppliers()
    target = _pick(rows, parts[0])
    if target is None:
        await message.answer("Не нашёл такого номера. Список: /suppliers")
        return

    await supplier_store.rename_supplier(target.id, parts[1])
    await message.answer(f"«{target.name}» переименован в «{parts[1].strip()}».")
    await _mirrors_know(model)


@router.message(Command("supplier_delete"))
async def cmd_supplier_delete(message: Message, command: CommandObject,
                              supplier_store: SupplierStore, is_admin: bool) -> None:
    if not is_admin:
        return await _deny(message)
    arg = (command.args or "").strip()
    rows = await supplier_store.list_suppliers()
    target = _pick(rows, arg)
    if target is None:
        await message.answer("Нужен номер из /suppliers, например: /supplier_delete 3")
        return

    if await supplier_store.delete_supplier(target.id):
        await message.answer(f"Поставщик «{target.name}» удалён.")
        return

    # Непустого не удаляем: его сигнатуры и файлы остались бы сиротами.
    sig = len(await supplier_store.list_signatures(target.id))
    await message.answer(
        f"У «{target.name}» есть сигнатуры ({sig}) — удалять нельзя, прайсы остались бы "
        f"без владельца.\nЕсли это дубль, слейте его: /supplier_merge {arg} <номер основного>")


@router.message(Command("supplier_merge"))
async def cmd_supplier_merge(message: Message, command: CommandObject,
                             supplier_store: SupplierStore, is_admin: bool,
                             model=None) -> None:
    if not is_admin:
        return await _deny(message)
    parts = (command.args or "").split()
    rows = await supplier_store.list_suppliers()
    source = _pick(rows, parts[0]) if parts else None
    target = _pick(rows, parts[1]) if len(parts) > 1 else None
    if source is None or target is None:
        await message.answer("Нужны два номера из /suppliers: сначала дубль, потом "
                             "основной.\nНапример: /supplier_merge 4 2")
        return
    if source.id == target.id:
        await message.answer("Это один и тот же поставщик.")
        return

    try:
        result = await supplier_store.merge_suppliers(source.id, target.id)
    except ValueError as exc:
        await message.answer(f"Не получилось: {exc}")
        return
    # ПАМЯТЬ МОДЕЛИ ПРАВИМ ДО ПРОБУЖДЕНИЯ ЗЕРКАЛ, иначе снимок уедет со старым кодом
    # поставщика — а 1С опознаёт элемент зеркала по коду и покажет его прежнее имя.
    if model is not None:
        model.supplier_merged(source.id, target.id)

    await message.answer(view.render_merge(result, source.name, target.name))
    await _mirrors_know(model)


# -------------------------------------------------------------------- сигнатуры

async def _mirrors_know(model) -> None:
    """Сказать зеркалам, что имена поставщиков поменялись.

    Форма 1С показывает имя поставщика в таблице прайсов, но снимок уходит туда, ТОЛЬКО
    когда состояние модели менялось, — а переименование живёт в справочнике и модели
    невидимо. Без этого админ переименовал поставщика, нажал в форме «Обновить» и увидел
    прежнее имя (бой 22.09.2026).

    Текст пуст намеренно: Telegram такие события пропускает, и админ не получает второго
    сообщения о том, что сам только что сделал командой.
    """
    if model is None:
        return
    try:
        await model.events.publish(Event(EventKind.SUPPLIER_RENAMED, text=""))
    except Exception:                                   # noqa: BLE001
        logger.warning("Зеркала не узнали о правке справочника", exc_info=True)


async def _signature_rows(store: SupplierStore, supplier_id: int | None = None):
    """Пары (сквозной номер, сигнатура). Фильтр прячет строки, но не меняет номера."""
    every = await store.list_signatures()
    rows = [(i, s) for i, s in enumerate(every, 1)
            if supplier_id is None or s.supplier_id == supplier_id]
    return every, rows


@router.message(Command("signatures"))
async def cmd_signatures(message: Message, command: CommandObject,
                         supplier_store: SupplierStore, is_admin: bool) -> None:
    if not is_admin:
        return await _deny(message)

    suppliers = await supplier_store.list_suppliers()
    only = _pick(suppliers, (command.args or "").strip())
    _, rows = await _signature_rows(supplier_store, only.id if only else None)

    names = {s.id: s.name for s in suppliers}
    files = {sid: n for sid, n in
             [(s.id, len(await supplier_store.list_price_files(s.id))) for _, s in rows]}
    tail = ("\n\nНомера сквозные по всему справочнику.\n"
            "Правка: /signature_move <номер> <номер поставщика>, /signature_delete <номер>"
            if rows else "")
    await message.answer(view.render_signatures(rows, names, files) + tail)


@router.message(Command("signature_move"))
async def cmd_signature_move(message: Message, command: CommandObject,
                             supplier_store: SupplierStore, is_admin: bool) -> None:
    if not is_admin:
        return await _deny(message)
    parts = (command.args or "").split()
    every = await supplier_store.list_signatures()
    suppliers = await supplier_store.list_suppliers()
    sig = _pick(every, parts[0]) if parts else None
    to = _pick(suppliers, parts[1]) if len(parts) > 1 else None
    if sig is None or to is None:
        await message.answer("Нужны номер сигнатуры из /signatures и номер поставщика "
                             "из /suppliers.\nНапример: /signature_move 3 1")
        return

    await supplier_store.move_signature(sig.id, to.id)
    await message.answer(f"Сигнатура «{view.signature_label(sig)}» теперь у «{to.name}».")


@router.message(Command("signature_delete"))
async def cmd_signature_delete(message: Message, command: CommandObject,
                               supplier_store: SupplierStore, is_admin: bool) -> None:
    if not is_admin:
        return await _deny(message)
    every = await supplier_store.list_signatures()
    sig = _pick(every, (command.args or "").strip())
    if sig is None:
        await message.answer("Нужен номер из /signatures, например: /signature_delete 2")
        return

    files = len(await supplier_store.list_price_files(sig.id))
    await supplier_store.delete_signature(sig.id)
    # Файлы с диска не трогаем: их судьбу решает уборка сирот при старте, и только когда
    # на них не ссылается ни один объект модели.
    await message.answer(
        f"Сигнатура «{view.signature_label(sig)}» убрана из справочника"
        + (f" вместе с записями о {files} файлах." if files else "."))


# ----------------------------------------------------------------- файлы прайсов

@router.message(Command("price_files"))
async def cmd_price_files(message: Message, command: CommandObject,
                          supplier_store: SupplierStore, is_admin: bool) -> None:
    if not is_admin:
        return await _deny(message)

    every_sig = await supplier_store.list_signatures()
    only = _pick(every_sig, (command.args or "").strip())

    every = await supplier_store.list_price_files()
    rows = [(i, f) for i, f in enumerate(every, 1)
            if only is None or f.signature_id == only.id]
    labels = {s.id: view.signature_label(s) for s in every_sig}
    tail = "\n\nНомера сквозные. Убрать запись: /price_file_delete <номер>" if rows else ""
    await message.answer(view.render_files(rows, labels) + tail)


@router.message(Command("price_file_delete"))
async def cmd_price_file_delete(message: Message, command: CommandObject,
                                supplier_store: SupplierStore, is_admin: bool) -> None:
    if not is_admin:
        return await _deny(message)
    every = await supplier_store.list_price_files()
    target = _pick(every, (command.args or "").strip())
    if target is None:
        await message.answer("Нужен номер из /price_files, например: /price_file_delete 1")
        return

    await supplier_store.delete_price_file(target.id)
    await message.answer(f"Запись о файле «{target.filename}» убрана. "
                         "Сам файл удалится уборкой при старте, если на него больше никто "
                         "не ссылается.")


# --------------------------------------------------- инвентаризация свойства «Коллекция»

@router.message(Command("empty_collections"))
async def cmd_empty_collections(message: Message, command: CommandObject,
                                onec, is_admin: bool) -> None:
    """Где в каталоге не проставлено свойство «Коллекция».

    ОБХОД ИДЁТ ПО МАРКАМ, потому что другого входа в номенклатуру нет: `by-tm` требует
    марку. Дорого это не по деньгам (модель не участвует вовсе), а по времени — минуты, —
    поэтому ход показывается прямо в сообщении: молчащая три минуты команда читается как
    зависшая.

    БЕЗ АРГУМЕНТА СМОТРИМ ТОЛЬКО ПОМЕЧЕННЫЕ К ВЫГРУЗКЕ марки: свойство нужно ради сайта, а
    непомеченная марка на сайт и не идёт. Назвали марку — смотрим её, помечена она или нет:
    раз спросили именно про неё, довод про сайт уже не при чём.
    """
    if not is_admin:
        return await _deny(message)
    if onec is None:
        await message.answer("Интеграция с 1С не настроена — смотреть нечего.")
        return

    wanted = (command.args or "").strip()
    marks = await asyncio.to_thread(onec.selling_tm, bool(wanted))
    if wanted:
        marks = [m for m in marks if wanted.lower() in m.name.lower()]
    if not marks:
        await message.answer(f"Марок по «{wanted}» не нашлось." if wanted
                             else "1С не отдала ни одной марки.")
        return

    status = await message.answer(f"Смотрю {len(marks)} марок — это займёт несколько минут.")
    gaps: list = []
    failed: list[str] = []
    lost = live = 0

    for number, mark in enumerate(marks, 1):
        await _say(status, f"Смотрю {number} из {len(marks)}: {mark.name}…")
        try:
            nom = await asyncio.to_thread(onec.by_tm_all, mark.code)
        except Exception as exc:                             # noqa: BLE001
            # ОДНА УПАВШАЯ МАРКА НЕ ХОРОНИТ ОБХОД: канал теряет запросы (см. CLAUDE.md про
            # туннель), и отчёт по двадцати девяти маркам полезнее, чем ошибка вместо всего.
            # Имя упавшей марки попадёт в отчёт — иначе её дыры сойдут за отсутствие дыр.
            logger.warning("Марка %s не выгрузилась: %s", mark.name, exc)
            failed.append(mark.name)
            continue
        live += sum(1 for i in nom.items if not i.not_exported)
        lost += len(nom.errors)
        gaps.extend(audit.scan(mark.name, nom.items))

    text = audit.render(gaps, marks=len(marks) - len(failed), live=live,
                        failed=failed, lost=lost, scope=wanted)
    for part in audit.split(text):
        await message.answer(part)


@router.message(Command("signature_sheets"))
async def cmd_signature_sheets(message: Message, command: CommandObject,
                               supplier_store: SupplierStore, is_admin: bool) -> None:
    """Какие листы разбирать у этого формата прайса.

    УКАЗАНИЕ ЖИВЁТ У СИГНАТУРЫ, а не у файла: следующий прайс того же поставщика придёт с
    теми же листами, и повторять указание на каждый файл незачем.

    Пустой список листов снимает ограничение — отдельной команды «разрешить всё» не надо:
    она отличалась бы от этой только отсутствием аргумента, то есть ничем.
    """
    if not is_admin:
        return await _deny(message)

    parts = (command.args or "").strip().split(maxsplit=1)
    every = await supplier_store.list_signatures()
    target = _pick(every, parts[0]) if parts else None
    if target is None:
        await message.answer(
            "Нужен номер из /signatures и листы через запятую:\n"
            "/signature_sheets 2 ЛАМИНАТ, SPC\n\n"
            "Посмотреть, какие листы есть: /signature_sheets 2\n"
            "Снять ограничение: /signature_sheets 2 -")
        return

    label = view.signature_label(target)

    # БЕЗ АРГУМЕНТА — ПОКАЗЫВАЕМ, ИЗ ЧЕГО ВЫБИРАТЬ. Имена листов знает только файл, и
    # набирать их по памяти — верный способ промахнуться: промах снимает ограничение
    # целиком, то есть молча возвращает разбор к полному.
    if len(parts) == 1:
        known = (target.sheet_list or "").strip()
        chosen = (target.sheets or "").strip()
        lines = [f"Формат «{label}»."]
        lines.append(f"Листы последнего файла: {known}" if known
                     else "Листы пока неизвестны — прайс этого формата ещё не приходил.")
        lines.append(f"Разбираем только: {chosen}" if chosen
                     else "Ограничения нет — разбираются все листы.")
        lines.append("")
        lines.append(f"Задать: /signature_sheets {parts[0]} ЛАМИНАТ, SPC")
        lines.append(f"Снять: /signature_sheets {parts[0]} -")
        await message.answer("\n".join(lines))
        return

    sheets = parts[1].strip()
    # Прочерк — снять. Отдельным словом, а не пустым аргументом: пустой аргумент теперь
    # показывает список, и два разных действия на одну запись команды путали бы.
    if sheets == "-":
        await supplier_store.set_signature_sheets(target.id, "")
        await message.answer(f"Ограничение снято: формат «{label}» будет разбираться "
                             "целиком, все листы.")
        return

    await supplier_store.set_signature_sheets(target.id, sheets)
    await message.answer(
        f"Формат «{label}»: разбираем только листы — {sheets}.\n\n"
        "Имена сверяются без учёта регистра и пробелов. Если ни один не совпадёт с файлом, "
        "ограничение в тот раз не применится, а агент скажет об этом в отчёте — "
        "молча разобрать ноль листов значило бы выдать «работы нет».")


# ------------------------------------------------------------ дайджест по фото

# Имя команды — как его задал админ (30.09.2026). Правильное написание принимается вторым:
# опечатка в имени команды оборачивается «команда не найдена», и человек решает, что
# функции нет вовсе.
@router.message(Command("no_photo_summury", "no_photo_summary"))
async def cmd_no_photo_summary(message: Message, command: CommandObject,
                               photo_watch) -> None:
    """Короткая сводка «сколько товаров ждут фото» — ДЛЯ ВСЕХ, кто работает с агентом.

    Проверки на админа здесь нет намеренно: фото добавляют менеджеры, им и нужно видеть,
    сколько работы осталось. Доступ к боту уже ограничен белым списком (`AuthMiddleware`),
    второй замок на ту же дверь только мешал бы.

    СЧИТАЕТСЯ ПО ЖУРНАЛУ, поэтому отвечает мгновенно и не трогает ни 1С, ни сайт: всё
    нужное посчитала ежедневная проверка. Полный список со ссылками — вопрос агенту
    «покажи, где не добавлены фото».
    """
    text, why = await audit_photos.from_journal(photo_watch,
                                                (command.args or "").strip())
    await message.answer(text or why)


# ------------------------------------------------- подписка на дайджест по фото

@router.message(Command("photo_subs"))
async def cmd_photo_subs(message: Message, photo_subscribers, store,
                         is_admin: bool) -> None:
    """Кто получает еженедельный дайджест «где не добавлены фото»."""
    if not is_admin:
        return await _deny(message)

    rows = await photo_subscribers.list_all()
    if not rows:
        await message.answer(
            "Дайджест по фото не получает никто. Подписать: "
            "/photo_sub_add <id> [имя]\n\n"
            "Пустой список значит «не слать никому» — в том числе администратору.")
        return

    names = {u.telegram_id: u.name for u in await store.list_all()}
    lines = [f"{i}. {r.name or names.get(r.telegram_id) or '—'} ({r.telegram_id})"
             for i, r in enumerate(rows, 1)]
    await message.answer("Дайджест по фото получают (по понедельникам):\n"
                         + "\n".join(lines)
                         + "\n\nПодписать: /photo_sub_add <id> [имя]"
                           "\nОтписать: /photo_sub_delete <номер>")


@router.message(Command("photo_sub_add"))
async def cmd_photo_sub_add(message: Message, command: CommandObject,
                            photo_subscribers, store, is_admin: bool) -> None:
    if not is_admin:
        return await _deny(message)

    parts = (command.args or "").split(maxsplit=1)
    if not parts or not parts[0].lstrip("-").isdigit():
        await message.answer("Укажите telegram-id: /photo_sub_add 1006193754 Иван")
        return

    who = int(parts[0])
    name = parts[1].strip() if len(parts) > 1 else ""

    # ПОДПИСАТЬ МОЖНО ТОЛЬКО ТОГО, У КОГО ЕСТЬ ДОСТУП К БОТУ: рассылка несёт наименования
    # и ссылки на карточки каталога, и отправить их человеку, которому пользоваться ботом
    # не разрешали, — значит выдать данные в обход белого списка.
    #
    # САМ СЕБЯ АДМИН ПОДПИСЫВАЕТ ВСЕГДА, и это не поблажка: `ADMIN_TELEGRAM_ID` проходит
    # авторизацию МИМО белого списка (`bot/auth.py`), и в нём админа обычно нет вовсе —
    # без этой ветки первая же команда «подпиши меня» отвечала бы «у вас нет доступа к
    # боту» тому, кто этим ботом и распоряжается.
    allowed = {u.telegram_id: u.name for u in await store.list_all()}
    himself = bool(message.from_user) and who == message.from_user.id
    if who not in allowed and not himself:
        await message.answer(
            f"У {who} нет доступа к боту — сперва /adduser {who}. "
            "Рассылка несёт ссылки на карточки каталога, и получать её должен только тот, "
            "кому и так разрешено пользоваться ботом.")
        return

    label = name or allowed.get(who) or "Администратор"
    added = await photo_subscribers.add(
        who, label, message.from_user.id if message.from_user else None)
    await message.answer(f"{'Подписан' if added else 'Уже был подписан'}: {label}. "
                         "Список: /photo_subs")


@router.message(Command("photo_sub_delete"))
async def cmd_photo_sub_delete(message: Message, command: CommandObject,
                               photo_subscribers, is_admin: bool) -> None:
    if not is_admin:
        return await _deny(message)

    rows = await photo_subscribers.list_all()
    arg = (command.args or "").strip()
    target = _pick(rows, arg)
    if target is None and arg.lstrip("-").isdigit():
        # Номер из списка удобнее, но id принимаем тоже: его видно в том же списке, и
        # заставлять пересчитывать строки незачем.
        target = next((r for r in rows if r.telegram_id == int(arg)), None)
    if target is None:
        await message.answer("Нужен номер из /photo_subs или telegram-id, "
                             "например: /photo_sub_delete 1")
        return

    await photo_subscribers.remove(target.telegram_id)
    await message.answer(f"Отписан: {target.name or target.telegram_id}. "
                         "Список: /photo_subs")


async def _say(status: Message, text: str) -> None:
    """Показать ход обхода. Правка статуса — удобство, а не работа: сорвалась — идём
    дальше, ронять из-за неё выгрузку, которая шла минуту, нельзя."""
    try:
        await status.edit_text(text)
    except Exception:                                        # noqa: BLE001
        pass
