"""Режим обновления цен: приём прайса документом и запись после подтверждения.

Гейт безопасности (§10 спеки): агент только ГОТОВИТ предложение и сохраняет payload;
`set-prices` вызывается ровно здесь, в обработчике кнопки, из сохранённого payload.
Модель не участвует в записи и не может её инициировать.
"""
import asyncio
import io
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.agent.pricing_tools import PRICING_TOOLS, PricingTools
from src.agent.prompts import PRICING_PROMPT
from src.storage.pricing import PricingStore

logger = logging.getLogger(__name__)

router = Router()

PRICE_EXTS = (".xlsx", ".xls", ".csv", ".pdf")
MAX_FILE_BYTES = 20 * 1024 * 1024

_STATUS = {
    "read_price_file": "Читаю прайс...",
    "get_selling_tm": "Проверяю выгрузку ТМ в 1С...",
    "get_1c_nomenclature": "Загружаю номенклатуру из 1С...",
    "propose_prices": "Считаю изменения и розницу...",
}

# Файл прайса живёт в памяти процесса на время диалога: в БД его класть незачем,
# а перечитывать из Telegram на каждый ход — лишние round-trip'ы.
_files: dict[int, tuple[str, bytes]] = {}


def _keyboard(proposal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Записать в 1С", callback_data=f"price:apply:{proposal_id}"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data=f"price:cancel:{proposal_id}"),
    ]])


async def _send(message: Message, text: str, markup=None) -> None:
    chunks = [text[i:i + 4096] for i in range(0, len(text), 4096)] or [""]
    for i, chunk in enumerate(chunks):
        await message.answer(chunk, reply_markup=markup if i == len(chunks) - 1 else None)


async def _run(message: Message, user_text: str, orchestrator, onec, store: PricingStore,
               status_msg: Message) -> None:
    user_id = message.from_user.id
    tools = PricingTools(onec, store, user_id)
    if user_id in _files:
        tools.set_file(*_files[user_id])

    async def on_tool(name: str, _inp: dict) -> None:
        try:
            await status_msg.edit_text(_STATUS.get(name, f"Выполняю {name}..."))
        except Exception:
            pass

    history = await store.load_messages(user_id)
    history.append({"role": "user", "content": user_text})

    try:
        answer, history = await orchestrator.handle_turn(
            history, on_tool=on_tool, system=PRICING_PROMPT,
            extra_tools=PRICING_TOOLS, extra_executor=tools)
    except Exception:
        logger.exception("Ошибка обработки прайса")
        await status_msg.edit_text("Ошибка при обработке прайса. Подробности в логах.")
        return

    await store.save_messages(user_id, history)
    try:
        await status_msg.delete()
    except Exception:
        pass

    pending = await store.get_pending(user_id)
    await _send(message, answer, _keyboard(pending.proposal_id) if pending else None)


@router.message(F.document)
async def handle_price_document(message: Message, orchestrator, onec, pricing_store: PricingStore,
                                is_admin: bool) -> None:
    if not is_admin:
        await message.answer("Обновление цен доступно только администратору.")
        return
    if onec is None:
        await message.answer("Интеграция с 1С не настроена: добавьте ONEC_BASE_URL и "
                             "ONEC_TOKEN в .env и перезапустите бота.")
        return

    doc = message.document
    if not doc.file_name.lower().endswith(PRICE_EXTS):
        await message.answer(f"Не похоже на прайс. Поддерживаются: {', '.join(PRICE_EXTS)}")
        return
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await message.answer("Файл слишком большой (лимит 20 МБ).")
        return

    status_msg = await message.answer("Скачиваю прайс...")
    buf = io.BytesIO()
    file_info = await message.bot.get_file(doc.file_id)
    await message.bot.download_file(file_info.file_path, destination=buf)
    _files[message.from_user.id] = (doc.file_name, buf.getvalue())
    await pricing_store.reset(message.from_user.id)   # новый прайс — новый диалог

    caption = (message.caption or "").strip()
    task = f"Прислан прайс «{doc.file_name}»." + (f" Комментарий админа: {caption}" if caption else "")
    await _run(message, task, orchestrator, onec, pricing_store, status_msg)


@router.message(Command("cancel_price"))
async def cmd_cancel(message: Message, pricing_store: PricingStore) -> None:
    _files.pop(message.from_user.id, None)
    await pricing_store.reset(message.from_user.id)
    await message.answer("Работа с прайсом прекращена, предложение отменено.")


@router.message(F.text, lambda m: m.from_user.id in _files)
async def handle_price_reply(message: Message, orchestrator, onec, pricing_store: PricingStore,
                             is_admin: bool) -> None:
    """Ответ админа внутри диалога по прайсу (уточнение колонки, бренда и т.п.)."""
    if not is_admin:
        return
    status_msg = await message.answer("Обрабатываю...")
    await _run(message, message.text, orchestrator, onec, pricing_store, status_msg)


@router.callback_query(F.data.startswith("price:"))
async def handle_price_decision(callback: CallbackQuery, onec, pricing_store: PricingStore,
                                is_admin: bool) -> None:
    _, action, raw_id = callback.data.split(":")
    user_id = callback.from_user.id
    if not is_admin:
        await callback.answer("Только администратор", show_alert=True)
        return

    if action == "cancel":
        await pricing_store.reject(user_id, int(raw_id))
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Отменено — цены не записаны.")
        await callback.answer()
        return

    proposal = await pricing_store.take_pending(user_id, int(raw_id))
    if proposal is None:
        await callback.answer("Предложение уже обработано или устарело", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Записываю...")
    progress = await callback.message.answer("Обновляю цены в 1С...")

    try:
        result = await asyncio.to_thread(onec.set_prices, proposal.payload)
    except Exception as exc:                       # noqa: BLE001
        logger.exception("Ошибка записи цен в 1С")
        # запись не состоялась — возвращаем предложение в очередь и кнопку админу,
        # чтобы не гонять разбор прайса заново из-за оборванного соединения
        await pricing_store.release(proposal.proposal_id)
        await progress.edit_text(
            f"Ошибка записи в 1С: {exc}\nЦены не обновлены — можно повторить.",
            reply_markup=_keyboard(proposal.proposal_id))
        return

    await pricing_store.mark_applied(proposal.proposal_id)
    await progress.edit_text(_format_result(result))

    # цены записаны — выходим из режима прайса, иначе следующий обычный вопрос
    # админа будет истолкован как ответ по прайсу
    _files.pop(user_id, None)
    await pricing_store.reset(user_id)
    await callback.message.answer(
        "Работа с прайсом завершена. Пришлите следующий файл, когда понадобится.")


def _format_result(result: dict) -> str:
    """Отчёт по ответу set-prices (§10.3): что записано, что пропущено, ошибки."""
    lines = [f"Готово, {result.get('date', '')}: обновлено {result.get('updated', 0)} поз."]
    if result.get("unchanged"):
        lines.append(f"Без изменений: {result['unchanged']} поз.")
    for res in result.get("results", []):
        if "changes" in res:
            head = f"• {res.get('tm_name', '')} / {res.get('collection', '')} — {res.get('count')} поз."
            lines.append(head)
            for ch in res["changes"]:
                mark = " (уже актуально)" if ch.get("skipped") else ""
                old = ch.get("old")
                lines.append(f"    {ch['price_type']}: "
                             f"{'нет' if old is None else old} → {ch.get('new')} "
                             f"— {ch.get('count')} поз.{mark}")
        else:
            for kind, det in (res.get("written") or {}).items():
                old = det.get("old")
                lines.append(f"• {res.get('name', res.get('ref'))} — {kind}: "
                             f"{'нет' if old is None else old} → {det.get('new')}")
    errors = result.get("errors") or []
    if errors:
        lines.append(f"\nОшибки ({len(errors)}):")
        for e in errors[:20]:
            lines.append(f"  ⚠️ {e.get('code')}: {e.get('message')}")
        if len(errors) > 20:
            lines.append(f"  ... и ещё {len(errors) - 20}")
    return "\n".join(lines)
