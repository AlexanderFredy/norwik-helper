"""Обработчики сообщений: админ-команды и запросы менеджера."""
import html
import io
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from src.storage.users import UserStore

logger = logging.getLogger(__name__)

ADMIN_ONLY = "Команда доступна только администратору"

router = Router()

_TOOL_STATUS: dict[str, str] = {
    "search_emails": "Ищу в почте поставщиков...",
    "read_attachment": "Читаю вложение...",
    "get_email_contacts": "Получаю контакты поставщика...",
    "search_norwik": "Ищу на сайте norwik.ru...",
    "get_norwik_product": "Проверяю карточку товара...",
    "web_search": "Ищу в интернете...",
}


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
        "и нужное количество — я найду поставщиков, остатки и цены.\n"
        "Можно отправить голосовое сообщение."
    )


async def _process_query(message: Message, text: str, orchestrator, status_msg) -> None:
    """Общая логика обработки запроса с обновлением статуса."""

    async def on_tool(name: str, _input: dict) -> None:
        label = _TOOL_STATUS.get(name, f"Выполняю {name}...")
        try:
            await status_msg.edit_text(label)
        except Exception:
            pass

    try:
        answer = await orchestrator.handle_query(text, on_tool=on_tool)
    except Exception:
        logger.exception("Ошибка обработки запроса")
        await status_msg.edit_text("Произошла ошибка при обработке запроса. Попробуйте позже.")
        return

    try:
        await status_msg.delete()
    except Exception:
        pass

    for i in range(0, len(answer), 4096):
        await message.answer(answer[i : i + 4096])


@router.message(F.voice)
async def handle_voice(message: Message, orchestrator, openai_api_key: str | None) -> None:
    if not openai_api_key:
        await message.answer(
            "Транскрипция голосовых сообщений не настроена. "
            "Добавьте OPENAI_API_KEY в .env и перезапустите бота."
        )
        return

    status_msg = await message.answer("Распознаю голосовое сообщение...")

    try:
        from openai import AsyncOpenAI

        file_info = await message.bot.get_file(message.voice.file_id)
        buf = io.BytesIO()
        await message.bot.download_file(file_info.file_path, destination=buf)
        buf.seek(0)
        buf.name = "voice.ogg"

        oai = AsyncOpenAI(api_key=openai_api_key)
        transcription = await oai.audio.transcriptions.create(
            model="whisper-1",
            file=buf,
            language="ru",
        )
        text = transcription.text.strip()
    except Exception:
        logger.exception("Ошибка транскрипции голосового сообщения")
        await status_msg.edit_text("Не удалось распознать голосовое сообщение. Попробуйте текстом.")
        return

    if not text:
        await status_msg.edit_text("Голосовое сообщение пустое или не распознано.")
        return

    await status_msg.edit_text(f"Распознано: {text}\n\nОбрабатываю запрос...")
    await _process_query(message, text, orchestrator, status_msg)


@router.message()
async def handle_query(message: Message, orchestrator) -> None:
    if not message.text:
        await message.answer("Пожалуйста, отправьте запрос текстом или голосовым сообщением")
        return
    status_msg = await message.answer("Обрабатываю запрос...")
    await _process_query(message, message.text, orchestrator, status_msg)
