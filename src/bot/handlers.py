"""Обработчики сообщений: админ-команды и запросы менеджера."""
import html
import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from src.storage.users import UserStore

logger = logging.getLogger(__name__)

ADMIN_ONLY = "Команда доступна только администратору"

router = Router()


def _parse_id(args: str | None) -> int | None:
    if not args:
        return None
    try:
        return int(args.strip().split()[0])
    except ValueError:
        return None


@router.message(Command("adduser"))
async def cmd_adduser(
    message: Message, command: CommandObject, store: UserStore, is_admin: bool
) -> None:
    if not is_admin:
        await message.answer(ADMIN_ONLY)
        return
    user_id = _parse_id(command.args)
    if user_id is None:
        await message.answer("Использование: /adduser <telegram_id> [имя]")
        return
    parts = (command.args or "").strip().split(maxsplit=1)
    name = parts[1] if len(parts) > 1 else None
    added = await store.add(user_id, added_by=message.from_user.id, name=name)
    if added:
        await message.answer(f"Пользователь {user_id} добавлен")
    else:
        await message.answer(f"Пользователь {user_id} уже в списке")


@router.message(Command("removeuser"))
async def cmd_removeuser(
    message: Message, command: CommandObject, store: UserStore, is_admin: bool
) -> None:
    if not is_admin:
        await message.answer(ADMIN_ONLY)
        return
    user_id = _parse_id(command.args)
    if user_id is None:
        await message.answer("Использование: /removeuser <telegram_id>")
        return
    removed = await store.remove(user_id)
    if removed:
        await message.answer(f"Пользователь {user_id} удалён")
    else:
        await message.answer(f"Пользователь {user_id} не найден в списке")


@router.message(Command("listusers"))
async def cmd_listusers(message: Message, store: UserStore, is_admin: bool) -> None:
    if not is_admin:
        await message.answer(ADMIN_ONLY)
        return
    users = await store.list_all()
    if not users:
        await message.answer("Список пользователей пуст")
        return
    lines = ["Разрешённые пользователи:"]
    for i, u in enumerate(users, 1):
        name = f" — {html.escape(u.name)}" if u.name else ""
        lines.append(f"{i}. {u.telegram_id}{name} (добавлен {u.added_at[:10]})")
    await message.answer("\n".join(lines))


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Я помощник менеджера по продажам.\n"
        "Напишите название товара (бренд, коллекция/артикул, для плитки — размер) "
        "и нужное количество — я найду поставщиков, остатки и цены."
    )


@router.message()
async def handle_query(message: Message, orchestrator) -> None:
    if not message.text:
        await message.answer("Пожалуйста, отправьте запрос текстом")
        return
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        answer = await orchestrator.handle_query(message.text)
    except Exception:
        logger.exception("Ошибка обработки запроса")
        await message.answer(
            "Произошла ошибка при обработке запроса. Попробуйте позже."
        )
        return
    # Telegram ограничивает сообщение 4096 символами
    for i in range(0, len(answer), 4096):
        await message.answer(answer[i : i + 4096])
