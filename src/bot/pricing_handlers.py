"""Режим обновления цен: приём прайса документом и запись после подтверждения.

Гейт безопасности (§10 спеки): агент только ГОТОВИТ предложение и сохраняет payload;
`set-prices` вызывается ровно здесь, в обработчике кнопки, из сохранённого payload.
Модель не участвует в записи и не может её инициировать.
"""
import asyncio
import io
import logging
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.agent.pricing_tools import PRICING_TOOLS, PricingTools
from src.agent.prompts import PRICING_PROMPT
from src.price_tool.broadcast import build_broadcast, journal_rows
from src.storage.pricing import PricingStore
from src.storage.users import UserStore

logger = logging.getLogger(__name__)

router = Router()

PRICE_EXTS = (".xlsx", ".xls", ".csv", ".pdf")
MAX_FILE_BYTES = 20 * 1024 * 1024

_STATUS = {
    "read_price_file": "Читаю прайс...",
    "save_price_mapping": "Запоминаю формат прайса...",
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


def _describe(entry: dict) -> str:
    m = entry["mapping"]
    parts = [f"закупка «{m.get('purchase_column')}»"]
    if m.get("rrc_column"):
        parts.append(f"РРЦ «{m['rrc_column']}»")
    if m.get("basis") and m["basis"] != "base_unit":
        parts.append(f"база: {m['basis']}")
    if m.get("sheet"):
        parts.append(f"лист «{m['sheet']}»")
    return ", ".join(parts)


@router.message(Command("mappings"))
async def cmd_mappings(message: Message, pricing_store: PricingStore, is_admin: bool) -> None:
    """Какие форматы прайсов агент уже разбирает без вопросов."""
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    entries = await pricing_store.list_mappings()
    if not entries:
        await message.answer("Запомненных форматов прайсов пока нет — вопрос о колонках "
                             "будет задан при первом прайсе.")
        return
    lines = ["Запомненные форматы прайсов:"]
    for i, e in enumerate(entries, 1):
        used = f", применён {e['uses']} раз" if e["uses"] else ", ещё не применялся"
        lines.append(f"{i}. {e['supplier'] or 'поставщик не назван'} — {_describe(e)} "
                     f"({e['updated_at'][:10]}{used})")
        if e["mapping"].get("note"):
            lines.append(f"   основание: {e['mapping']['note']}")
    lines.append("\nЗабыть: /mapping_forget <номер> — тогда следующий прайс этого формата "
                 "снова спросит про колонки.")
    await _send(message, "\n".join(lines))


@router.message(Command("mapping_forget"))
async def cmd_mapping_forget(message: Message, command: CommandObject,
                             pricing_store: PricingStore, is_admin: bool) -> None:
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Укажите номер из /mappings, например: /mapping_forget 1")
        return

    entries = await pricing_store.list_mappings()
    target = None
    if arg.isdigit() and 1 <= int(arg) <= len(entries):
        target = entries[int(arg) - 1]
    else:                                   # допускаем и саму сигнатуру (её видно в БД)
        target = next((e for e in entries if e["signature"].startswith(arg.lower())), None)
    if target is None:
        await message.answer("Не нашёл такой записи. Посмотрите список: /mappings")
        return

    await pricing_store.forget_mapping(target["signature"])
    await message.answer(
        f"Забыл формат «{target['supplier'] or 'без названия'}» ({_describe(target)}).\n"
        "Следующий прайс этого формата снова спросит, какую колонку считать закупкой.")


def _is_price_reply(message: Message) -> bool:
    """Текст внутри открытого диалога по прайсу.

    Команды исключены: иначе `/help`, `/start` и прочие уйдут модели как ответ на её
    вопрос о колонках. Команды самого режима цен зарегистрированы выше и до сюда не
    доходят, но остальные живут в общем роутере — за этим фильтром.
    """
    return (message.from_user.id in _files
            and bool(message.text) and not message.text.startswith("/"))


@router.message(F.text, _is_price_reply)
async def handle_price_reply(message: Message, orchestrator, onec, pricing_store: PricingStore,
                             is_admin: bool) -> None:
    """Ответ админа внутри диалога по прайсу (уточнение колонки, бренда и т.п.)."""
    if not is_admin:
        return
    status_msg = await message.answer("Обрабатываю...")
    await _run(message, message.text, orchestrator, onec, pricing_store, status_msg)


def _written_on(result: dict) -> str:
    """День записи в терминах регистра цен 1С — им же датируется журнал."""
    raw = str(result.get("date") or "")
    return raw[:10] if len(raw) >= 10 and raw[4:5] == "-" else date.today().isoformat()


async def _notify_managers(bot, store: UserStore, digest: dict, result: dict,
                           admin_id: int) -> int:
    """Короткое уведомление менеджерам (п.6 ТЗ). Админ его не получает — у него отчёт.

    Позиции с ошибками 1С исключаются: сообщить об изменении цены, которая не записалась,
    хуже, чем не сообщить вовсе.
    """
    failed = {e.get("ref") for e in (result.get("errors") or []) if e.get("ref")}
    text = build_broadcast(digest, failed)
    if not text:
        return 0
    sent = 0
    for user in await store.list_all():
        if user.telegram_id == admin_id:
            continue
        try:
            await bot.send_message(user.telegram_id, text)
            sent += 1
        except Exception:                          # заблокировал бота, удалил чат и т.п.
            logger.warning("Не доставлено менеджеру %s", user.telegram_id, exc_info=True)
    return sent


@router.callback_query(F.data.startswith("price:"))
async def handle_price_decision(callback: CallbackQuery, onec, pricing_store: PricingStore,
                                store: UserStore, is_admin: bool) -> None:
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
    report = _format_result(result)

    # журнал и рассылка (п.6): 1С хранит дату изменения, но не источник цены —
    # «из какого прайса» знаем только мы, и только отсюда
    if proposal.digest:
        failed = {e.get("ref") for e in (result.get("errors") or []) if e.get("ref")}
        try:
            await pricing_store.record_writes(
                journal_rows(proposal.digest, _written_on(result), failed))
        except Exception:
            logger.exception("Не удалось записать журнал цен")   # цены в 1С уже записаны
        sent = await _notify_managers(callback.message.bot, store, proposal.digest,
                                      result, user_id)
        if sent:
            report += f"\n\nМенеджерам отправлено уведомление: {sent}."

    await progress.edit_text(report)

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
