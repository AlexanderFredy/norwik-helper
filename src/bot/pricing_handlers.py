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

from src.agent.pricing_tools import PRICING_TOOLS, PricingTools, clear_nomenclature_cache
from src.agent.prompts import PRICING_PROMPT
from src.bot.errors import describe_api_error
from src.price_tool.broadcast import build_broadcast, journal_rows
from src.price_tool.exclusive import resolve
from src.storage.pricing import PricingStore
from src.storage.users import UserStore

logger = logging.getLogger(__name__)

router = Router()

PRICE_EXTS = (".xlsx", ".xls", ".csv", ".pdf")
MAX_FILE_BYTES = 20 * 1024 * 1024

_STATUS = {
    "read_price_file": "Читаю прайс...",
    "get_product_scope": "Смотрю, какие категории анализируем...",
    "save_price_mapping": "Запоминаю формат прайса...",
    "get_selling_tm": "Проверяю выгрузку ТМ в 1С...",
    "get_1c_nomenclature": "Загружаю номенклатуру из 1С...",
    "propose_prices": "Считаю изменения и розницу...",
    "record_exclusives": "Запоминаю эксклюзивы поставщика...",
    "set_exclusive": "Запоминаю решение по эксклюзиву...",
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
    except Exception as exc:                           # noqa: BLE001
        logger.exception("Ошибка обработки прайса")
        await status_msg.edit_text(describe_api_error(
            exc, "Ошибка при обработке прайса. Подробности в логах."))
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
    clear_nomenclature_cache()                        # и свежие цены из 1С

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
    parts = []
    if entry.get("sheet"):
        parts.append(f"лист «{entry['sheet']}»")
    parts.append(f"закупка «{m.get('purchase_column')}»")
    if m.get("rrc_column"):
        parts.append(f"РРЦ «{m['rrc_column']}»")
    if m.get("basis") and m["basis"] != "base_unit":
        parts.append(f"база: {m['basis']}")
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

    await pricing_store.forget_mapping(target["signature"], target.get("sheet", ""))
    await message.answer(
        f"Забыл формат «{target['supplier'] or 'без названия'}» ({_describe(target)}).\n"
        "Трактовки других листов этого прайса остались. Следующий прайс этого формата "
        "снова спросит, какую колонку считать закупкой на этом листе.")


_SCOPE_HELP = ("\nДобавить: /category_add ламинат, керамическая плитка\n"
               "Убрать: /category_remove плинтус\n"
               "Список действует для всех прайсов сразу, повторять его в каждом файле "
               "не нужно. Названия сверяются нестрого: «плитка» покроет «Керамическая "
               "плитка» из 1С.")


@router.message(Command("categories"))
async def cmd_categories(message: Message, pricing_store: PricingStore, is_admin: bool) -> None:
    """Категории товаров, которые агент вообще анализирует (§6.8)."""
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    scope = await pricing_store.list_scope()
    if not scope:
        await message.answer(
            "Ограничений по категориям нет — агент разбирает все разделы прайса.\n"
            "Чтобы он не тратил время на плинтус, подложку, аксессуары и стеновые "
            "панели, перечислите то, что вам нужно." + _SCOPE_HELP)
        return
    lines = ["Анализируем только эти категории:"]
    lines += [f"{i}. {c['category']}" for i, c in enumerate(scope, 1)]
    lines.append("\nОстальные разделы прайса агент не разбирает — только коротко "
                 "перечисляет, что они в прайсе есть." + _SCOPE_HELP)
    await _send(message, "\n".join(lines))


@router.message(Command("category_add"))
async def cmd_category_add(message: Message, command: CommandObject,
                           pricing_store: PricingStore, is_admin: bool) -> None:
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    raw = (command.args or "").strip()
    if not raw:
        await message.answer("Укажите категории через запятую, например:\n"
                             "/category_add ламинат, керамическая плитка, обои")
        return
    names = [p.strip() for p in raw.split(",") if p.strip()]
    was_empty = not await pricing_store.list_scope()
    added = await pricing_store.add_scope(names)
    if not added:
        await message.answer("Эти категории уже в списке — /categories покажет текущий.")
        return
    text = f"Добавил: {', '.join(added)}."
    if was_empty:
        text += ("\n⚠️ Список был пуст, а это значило «анализируем всё». Теперь агент "
                 "разбирает ТОЛЬКО перечисленное — проверьте /categories, чтобы не "
                 "потерять нужный вид товара.")
    await message.answer(text)


@router.message(Command("category_remove"))
async def cmd_category_remove(message: Message, command: CommandObject,
                              pricing_store: PricingStore, is_admin: bool) -> None:
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    name = (command.args or "").strip()
    if not name:
        await message.answer("Укажите категорию, например: /category_remove плинтус")
        return
    if not await pricing_store.remove_scope(name):
        await message.answer(f"Категории «{name}» в списке нет — посмотрите /categories.")
        return
    left = await pricing_store.list_scope()
    text = f"Убрал «{name}»."
    if not left:
        text += ("\n⚠️ Список опустел — это снова означает «анализируем всё», а не "
                 "«ничего не анализируем».")
    await message.answer(text)


async def _exclusive_entries(store: PricingStore) -> list[dict]:
    """Пометки и споры одним пронумерованным списком — для /exclusives и /exclusive_forget."""
    active, disputes = resolve(*await store.load_exclusives())
    entries = [{"key": (e.tm_code, e.collection_ref, e.item_ref), "exc": e, "dispute": None}
               for e in sorted(active.values(),
                               key=lambda e: (e.tm_name or e.tm_code, e.collection))]
    entries += [{"key": (d.tm_code, d.collection_ref, d.item_ref), "exc": None, "dispute": d}
                for d in disputes]
    return entries


@router.message(Command("exclusives"))
async def cmd_exclusives(message: Message, pricing_store: PricingStore, is_admin: bool) -> None:
    """Эксклюзивы поставщиков (§9.5): что помечено и что ждёт решения."""
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    entries = await _exclusive_entries(pricing_store)
    if not entries:
        await message.answer(
            "Эксклюзивов пока нет. Они появляются сами, когда в прайсе встречается "
            "надпись «эксклюзив» — отдельной колонкой, заголовком раздела или в имени листа.")
        return

    lines = ["Эксклюзивы поставщиков (пометка справочная, на цены не влияет):"]
    for i, entry in enumerate(entries, 1):
        if entry["dispute"]:
            d = entry["dispute"]
            lines.append(f"{i}. ⚠️ {d.title} — спорят: {', '.join(d.suppliers)}. "
                         "Пометка не показывается, пока не решено.")
            for c in d.claims:
                lines.append(f"     {c.supplier}: «{c.phrase}» (прайс от {c.price_date})")
            continue
        e = entry["exc"]
        title = e.item_name or " / ".join(p for p in (e.tm_name or e.tm_code, e.collection) if p)
        how = "решение админа" if e.by_admin else f"прайс от {e.since}"
        lines.append(f"{i}. {title} — {e.supplier} ({how})")
    lines.append("\nСнять пометку: /exclusive_forget <номер>. Чтобы назначить эксклюзив "
                 "или разрешить спор, скажите об этом при разборе прайса.")
    await _send(message, "\n".join(lines))


@router.message(Command("exclusive_forget"))
async def cmd_exclusive_forget(message: Message, command: CommandObject,
                               pricing_store: PricingStore, is_admin: bool) -> None:
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    arg = (command.args or "").strip()
    entries = await _exclusive_entries(pricing_store)
    if not arg.isdigit() or not 1 <= int(arg) <= len(entries):
        await message.answer("Укажите номер из /exclusives, например: /exclusive_forget 1")
        return

    tm_code, collection_ref, item_ref = entries[int(arg) - 1]["key"]
    # решение «эксклюзива нет», а не удаление заявок: иначе следующий прайс с той же
    # надписью вернёт пометку обратно
    await pricing_store.set_exclusive_decision(tm_code, collection_ref, item_ref,
                                               supplier=None, note="снято админом")
    await message.answer("Пометка снята. Заявки поставщиков в прайсах на неё больше не влияют "
                         "— вернуть можно, сказав об этом при разборе прайса.")


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
                           admin_id: int, exclusives: dict | None = None) -> int:
    """Короткое уведомление менеджерам (п.6 ТЗ). Админ его не получает — у него отчёт.

    Позиции с ошибками 1С исключаются: сообщить об изменении цены, которая не записалась,
    хуже, чем не сообщить вовсе.
    """
    failed = {e.get("ref") for e in (result.get("errors") or []) if e.get("ref")}
    text = build_broadcast(digest, failed, exclusives)
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
        active, _ = resolve(*await pricing_store.load_exclusives())
        sent = await _notify_managers(callback.message.bot, store, proposal.digest,
                                      result, user_id, active)
        if sent:
            report += f"\n\nМенеджерам отправлено уведомление: {sent}."

    await progress.edit_text(report)

    # цены записаны — выходим из режима прайса, иначе следующий обычный вопрос
    # админа будет истолкован как ответ по прайсу
    _files.pop(user_id, None)
    await pricing_store.reset(user_id)
    clear_nomenclature_cache()      # цены в 1С изменились — кэш устарел
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
