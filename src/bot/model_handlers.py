"""Telegram как визуал модели (§1, §7–§9 specs/agent-workflow-model.md).

**Обработчики не действуют сами** — они кладут команду в очередь и будят цикл. Это и есть
требование «визуал только шлёт команды»: всё состояние меняет агент, в одном месте и по
одним правилам.

Исключение ровно одно — приём файла: он не команда, а поступление данных, и обрабатывается
сразу, чтобы не таскать содержимое файла через очередь.

Уведомления приходят обратно через `TelegramListener`: текст формирует код модели, здесь
только отправка.
"""
from __future__ import annotations

import io
import logging

from aiogram import F, Router
from aiogram.filters import Command as CommandFilter
from aiogram.filters import CommandObject
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from src.model.commands import Command, CommandKind
from src.model.enums import PriceStatus
from src.model.events import Event, EventKind, Listener
from src.price_tool import model_view as view

logger = logging.getLogger(__name__)

router = Router()

MAX_FILE_BYTES = 20 * 1024 * 1024
PRICE_EXTS = (".xlsx", ".xls", ".csv", ".pdf")


class TelegramListener(Listener):
    """Отправляет события админам. Текст уже готов — здесь только доставка."""

    def __init__(self, bot, chat_ids) -> None:
        self._bot = bot
        self._chats = list(chat_ids)

    async def notify(self, event: Event) -> None:
        # ПУСТОЙ ТЕКСТ — СОБЫТИЕ НЕ ДЛЯ ЧЕЛОВЕКА, а для зеркал: так переименование
        # поставщика будит снимок в 1С, не присылая админу второе сообщение о том, что
        # он и так только что сделал командой.
        if not (event.text or "").strip():
            return
        # Сводку по прайсу этот слушатель не трогает: у неё другие получатели и свой
        # слушатель (`ManagerListener`), а админу он отправит короткую строку о доставке.
        # Иначе админ получил бы весь текст дважды.
        if event.kind == EventKind.PRICE_SUMMARY:
            return
        for chat in self._chats:
            await self._bot.send_message(chat, event.text)


class ManagerListener(Listener):
    """Сводка по прайсу — МЕНЕДЖЕРАМ (решение админа 28.09.2026).

    Единственное событие, адресованное не админу. Получатели — все из белого списка,
    кроме него: отдельной роли «менеджер» в базе нет, и заводить её ради одной рассылки
    незачем — список доступа и есть список тех, кто работает с ботом.

    **АДМИНУ УХОДИТ ОТЧЁТ О ДОСТАВКЕ.** Менеджер, ни разу не писавший боту, сообщения не
    получит — Telegram не разрешает писать первым. Раньше такие случаи оседали в логе, и
    админ считал, что все оповещены.
    """

    def __init__(self, bot, users, admin_id: int) -> None:
        self._bot = bot
        self._users = users
        self._admin = admin_id

    async def notify(self, event: Event) -> None:
        if event.kind != EventKind.PRICE_SUMMARY or not (event.text or "").strip():
            return

        try:
            everyone = await self._users.list_all()
        except Exception:                               # noqa: BLE001
            logger.warning("Список получателей не прочитался", exc_info=True)
            return

        managers = [u for u in everyone if u.telegram_id != self._admin]
        sent, failed = 0, []
        for user in managers:
            try:
                await self._bot.send_message(user.telegram_id, event.text)
                sent += 1
            except Exception:                           # noqa: BLE001
                failed.append(user.name or str(user.telegram_id))
                logger.warning("Сводка не доставлена менеджеру %s", user.telegram_id,
                               exc_info=True)

        if not managers:
            note = "Сводка не отправлена: менеджеров в списке доступа нет."
        else:
            note = f"Сводка отправлена менеджерам: {sent} из {len(managers)}."
            if failed:
                note += (" Не доставлено: " + ", ".join(failed[:10])
                         + ". Такому получателю нужно самому написать боту хотя бы раз.")
        try:
            await self._bot.send_message(self._admin, note)
        except Exception:                               # noqa: BLE001
            logger.warning("Отчёт о рассылке не доставлен админу", exc_info=True)


class TelegramProvider:
    """Провайдер для цикла. Команды уже лежат в очереди — собирать нечего.

    Метод оставлен, чтобы интерфейс провайдера был единым (§9): у 1С здесь будет запрос к
    её очереди команд и измерение смещения часов.
    """

    async def collect(self, queue) -> None:
        return None


async def _send(message: Message, command: Command, loop, queue) -> None:
    """Положить команду в очередь и разбудить цикл.

    Смещение часов нулевое: метку ставит этот же процесс (§7.1).
    """
    await queue.put(command)
    loop.wake()


def _actor(message: Message) -> str:
    return str(message.from_user.id) if message.from_user else ""


def _number(arg: str) -> int | None:
    arg = (arg or "").strip()
    return int(arg) if arg.isdigit() else None


# ------------------------------------------------------------------ приём файла

@router.message(F.document)
async def handle_document(message: Message, model, is_admin: bool,
                          force: bool = False) -> None:
    if not is_admin:
        await message.answer("Обработка прайсов доступна только администратору.")
        return

    doc = message.document
    if not doc.file_name.lower().endswith(PRICE_EXTS):
        await message.answer(f"Не похоже на прайс. Поддерживаются: {', '.join(PRICE_EXTS)}")
        return
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await message.answer("Файл слишком большой (лимит 20 МБ).")
        return

    note = await message.answer("Принимаю прайс...")
    buf = io.BytesIO()
    info = await message.bot.get_file(doc.file_id)
    await message.bot.download_file(info.file_path, destination=buf)

    # Подпись к файлу — главный признак поставщика (§2.2).
    await model.submit(buf.getvalue(), doc.file_name,
                       supplier_hint=(message.caption or "").strip(),
                       force=force, actor=_actor(message))
    try:
        await note.delete()
    except Exception:                                   # noqa: BLE001
        pass


@router.message(CommandFilter("model_force"))
async def cmd_force(message: Message, model, is_admin: bool) -> None:
    """Принудительный приём: признак поступления, известный ДО проверки свежести (§4)."""
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    if not message.document:
        await message.answer("Пришлите файл прайса вместе с командой /model_force "
                             "(подписью к файлу).")
        return
    await handle_document(message, model, is_admin, force=True)


# --------------------------------------------------------------------- чтение

@router.message(CommandFilter("prices"))
async def cmd_prices(message: Message, model, supplier_store, is_admin: bool) -> None:
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    names = {s.id: s.name for s in await supplier_store.list_suppliers()}
    locks = {p.id: model.lock_of(p.id) for p in model.prices if model.lock_of(p.id)}
    await message.answer(view.render_prices(model.prices, locks, names))


@router.message(CommandFilter("tasks"))
async def cmd_tasks(message: Message, command: CommandObject, model,
                    supplier_store, is_admin: bool) -> None:
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    price_id = _number(command.args or "")
    price = model.price(price_id) if price_id else None
    if price is None:
        await message.answer("Нужен номер прайса из /prices, например: /tasks 1")
        return
    supplier = await supplier_store.get_supplier(price.supplier_price.supplier_id)
    await message.answer(view.render_tasks(price, supplier.name if supplier else "",
                                           model.lock_of(price.id)))


# --------------------------------------------------------------------- команды

@router.message(CommandFilter("run"))
async def cmd_run(message: Message, command: CommandObject, model, queue,
                  loop, is_admin: bool) -> None:
    if not is_admin:
        return
    task_id = _number(command.args or "")
    price, task = model.task(task_id) if task_id else (None, None)
    if task is None:
        await message.answer("Нужен номер задачи из /tasks, например: /run 3")
        return
    await _send(message, Command(kind=CommandKind.EXECUTE_TASK, source="telegram",
                                 actor=_actor(message), price_id=price.id,
                                 task_id=task.id), loop, queue)


@router.message(CommandFilter("edit"))
async def cmd_edit(message: Message, command: CommandObject, model, queue,
                   loop, is_admin: bool) -> None:
    if not is_admin:
        return
    parts = (command.args or "").strip().split(maxsplit=1)
    task_id = _number(parts[0]) if parts else None
    price, task = model.task(task_id) if task_id else (None, None)
    if task is None or len(parts) < 2:
        await message.answer("Нужен номер задачи и текст: /edit 3 сверить размеры")
        return
    await _send(message, Command(kind=CommandKind.EDIT_TASK_DESCRIPTION, source="telegram",
                                 actor=_actor(message), price_id=price.id,
                                 task_id=task.id, payload={"description": parts[1]}), loop, queue)


@router.message(CommandFilter("status"))
async def cmd_status(message: Message, command: CommandObject, model, queue,
                     loop, is_admin: bool) -> None:
    if not is_admin:
        return
    parts = (command.args or "").strip().split(maxsplit=1)
    task_id = _number(parts[0]) if parts else None
    price, task = model.task(task_id) if task_id else (None, None)
    if task is None or len(parts) < 2:
        await message.answer("Нужен номер задачи и статус: /status 3 выполнена\n"
                             "Допустимо: к обработке, выполнена, частично обработана")
        return
    await _send(message, Command(kind=CommandKind.SET_TASK_STATUS, source="telegram",
                                 actor=_actor(message), price_id=price.id,
                                 task_id=task.id, payload={"status": parts[1].strip()}),
                loop, queue)


@router.message(CommandFilter("task_delete"))
async def cmd_task_delete(message: Message, command: CommandObject, model, queue,
                          loop, is_admin: bool) -> None:
    if not is_admin:
        return
    task_id = _number(command.args or "")
    price, task = model.task(task_id) if task_id else (None, None)
    if task is None:
        await message.answer("Нужен номер задачи из /tasks")
        return
    await _send(message, Command(kind=CommandKind.DELETE_TASK, source="telegram",
                                 actor=_actor(message), price_id=price.id,
                                 task_id=task.id), loop, queue)


@router.message(CommandFilter("price_status"))
async def cmd_price_status(message: Message, command: CommandObject, model, queue,
                           loop, is_admin: bool) -> None:
    if not is_admin:
        return
    parts = (command.args or "").strip().split(maxsplit=1)
    price_id = _number(parts[0]) if parts else None
    if model.price(price_id) is None or len(parts) < 2:
        await message.answer("Нужен номер прайса и статус: /price_status 1 выполнен\n"
                             "Допустимо: к обработке, частично обработан, выполнен")
        return
    status = parts[1].strip()

    # ЗАКРЫТИЕ СПРАШИВАЕТСЯ И ЗДЕСЬ (решение админа 28.09.2026). За «выполнен» стоит
    # рассылка сводки менеджерам, и вопрос обязан звучать одинаково из обоих визуалов:
    # одно и то же действие не может давать разный результат в зависимости от того,
    # откуда нажали. Команда уйдёт только после «Да» — служба без подтверждения откажет.
    if status.casefold() == PriceStatus.DONE.value:
        await message.answer(
            f"Прайс №{price_id}. Вы уверены, что закончили работать с этим прайсом и "
            f"хотите оповестить менеджеров о результатах?",
            reply_markup=_done_keyboard(price_id))
        return

    await _send(message, Command(kind=CommandKind.SET_PRICE_STATUS, source="telegram",
                                 actor=_actor(message), price_id=price_id,
                                 payload={"status": status}), loop, queue)


def _done_keyboard(price_id: int) -> InlineKeyboardMarkup:
    """Да/Нет под вопросом о закрытии прайса.

    «Нет» отменяет СМЕНУ СТАТУСА, а не только рассылку (решение админа): вопрос
    спрашивает про «закончили ли», и отрицательный ответ означает, что работа
    продолжается.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data=f"model:done:{price_id}"),
        InlineKeyboardButton(text="Нет", callback_data="model:done:cancel")]])


@router.callback_query(F.data.startswith("model:done:"))
async def on_done_answer(call: CallbackQuery, model, queue, loop) -> None:
    """Ответ на вопрос о закрытии прайса."""
    tail = call.data.split(":")[-1]
    if tail == "cancel":
        await call.message.edit_text("Статус не менял — работа с прайсом продолжается.")
        await call.answer()
        return

    price_id = _number(tail)
    if price_id is None or model.price(price_id) is None:
        await call.message.edit_text("Прайс не найден — возможно, его уничтожили.")
        await call.answer()
        return

    await _send(call.message, Command(
        kind=CommandKind.SET_PRICE_STATUS, source="telegram",
        actor=str(call.from_user.id) if call.from_user else "",
        price_id=price_id,
        payload={"status": PriceStatus.DONE.value, "confirmed": True}), loop, queue)
    await call.message.edit_text(f"Закрываю прайс №{price_id} и оповещаю менеджеров.")
    await call.answer()


@router.message(CommandFilter("rebuild"))
async def cmd_rebuild(message: Message, command: CommandObject, model, queue,
                      loop, is_admin: bool) -> None:
    if not is_admin:
        return
    price_id = _number(command.args or "")
    if model.price(price_id) is None:
        await message.answer("Нужен номер прайса из /prices")
        return
    await _send(message, Command(kind=CommandKind.REBUILD_TASKS, source="telegram",
                                 actor=_actor(message), price_id=price_id), loop, queue)


@router.message(CommandFilter("price_delete"))
async def cmd_price_delete(message: Message, command: CommandObject, model, queue,
                           loop, is_admin: bool) -> None:
    if not is_admin:
        return
    price_id = _number(command.args or "")
    if model.price(price_id) is None:
        await message.answer("Нужен номер прайса из /prices")
        return
    await _send(message, Command(kind=CommandKind.DESTROY_PRICE, source="telegram",
                                 actor=_actor(message), price_id=price_id), loop, queue)


@router.message(CommandFilter("unlock"))
async def cmd_unlock(message: Message, command: CommandObject, model, queue,
                     loop, is_admin: bool) -> None:
    if not is_admin:
        return
    price_id = _number(command.args or "")
    if model.price(price_id) is None:
        await message.answer("Нужен номер прайса из /prices")
        return
    await _send(message, Command(kind=CommandKind.RELEASE_LOCK, source="telegram",
                                 actor=_actor(message), price_id=price_id), loop, queue)
