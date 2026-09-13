"""Команды справочников поставщиков, сигнатур и файлов (§2.3 spec/agent-workflow-model.md).

Отдельный роутер, а не добавка в `pricing_handlers`: там уже полторы тысячи строк про разбор
прайсов, и справочники к нему отношения не имеют.

**Номера сквозные по всему справочнику.** Админ набирает номер из списка, и если бы
нумерация зависела от фильтра, `/signature_delete 2` удалял бы разное в разных видах. Поэтому
номер — позиция в ПОЛНОМ списке, а фильтр только прячет лишние строки.
"""
import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from src.price_tool import catalog_view as view
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
                              supplier_store: SupplierStore, is_admin: bool) -> None:
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
                             supplier_store: SupplierStore, is_admin: bool) -> None:
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
    await message.answer(view.render_merge(result, source.name, target.name))


# -------------------------------------------------------------------- сигнатуры

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
