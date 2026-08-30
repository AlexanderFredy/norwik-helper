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

from src.agent.pricing_tools import (
    PRICING_TOOLS, PricingTools, clear_nomenclature_cache, next_step, queue_tail,
)
from src.agent.prompts import PRICING_PROMPT
from src.bot.errors import describe_api_error
from src.price_tool.broadcast import build_broadcast, journal_rows
from src.price_tool.exclusive import resolve
from src.price_tool.history import LABELS
from src.storage import price_files
from src.storage.pricing import PricingStore
from src.storage.users import UserStore

logger = logging.getLogger(__name__)

router = Router()

PRICE_EXTS = (".xlsx", ".xls", ".csv", ".pdf")
MAX_FILE_BYTES = 20 * 1024 * 1024

_STATUS = {
    "read_price_file": "Читаю прайс...",
    "get_product_scope": "Смотрю, какие категории анализируем...",
    "start_price_run": "Составляю план по маркам...",
    "start_tm_collections": "Разбиваю марку на коллекции...",
    "defer_task": "Откладываю задачу...",
    "add_final_note": "Откладываю замечание к итогу...",
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


def _keyboard(proposal_id: int, in_stage: bool = False) -> InlineKeyboardMarkup:
    """Кнопки под предложением (§9.7).

    «Отмена» отклоняет предложение и ОСТАВЛЯЕТ на этом шаге: админ хочет переспросить
    агента. «Пропустить» шаг закрывает и двигает очередь, «Отложить» вдобавок заводит
    задачу на возврат.

    Когда марка разбита на коллекции (`in_stage`), «Отложить» без уточнения двусмысленна —
    коллекцию или всю марку? — поэтому кнопки две. В режиме марок откладывать можно только
    марку целиком, и вторая кнопка не нужна.
    """
    rows = [[InlineKeyboardButton(text="✅ Записать в 1С",
                                  callback_data=f"price:apply:{proposal_id}"),
             InlineKeyboardButton(text="⏭ Пропустить",
                                  callback_data=f"price:skip:{proposal_id}")]]
    if in_stage:
        rows.append([
            InlineKeyboardButton(text="🕐 Отложить коллекцию",
                                 callback_data=f"price:defer:{proposal_id}"),
            InlineKeyboardButton(text="🕐 Отложить марку",
                                 callback_data=f"price:defer_tm:{proposal_id}")])
    else:
        rows.append([InlineKeyboardButton(text="🕐 Отложить марку целиком",
                                          callback_data=f"price:defer_tm:{proposal_id}")])
    rows.append([InlineKeyboardButton(text="✖️ Отмена (остаться на текущей задаче)",
                                      callback_data=f"price:cancel:{proposal_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _in_stage(store: PricingStore, user_id: int) -> bool:
    """Идём ли сейчас по коллекциям внутри марки."""
    run = await store.get_run(user_id)
    return bool((run or {}).get("stage"))


def _chunks(text: str) -> list[str]:
    """Разбивка под лимит сообщения Telegram (4096)."""
    return [text[i:i + 4096] for i in range(0, len(text), 4096)] or [""]


async def _send(message: Message, text: str, markup=None) -> None:
    parts = _chunks(text)
    for i, chunk in enumerate(parts):
        await message.answer(chunk, reply_markup=markup if i == len(parts) - 1 else None)


async def _deliver(progress: Message, message: Message, text: str) -> None:
    """Показать отчёт админу, чего бы это ни стоило.

    Вызывается ПОСЛЕ записи цен в 1С, то есть после необратимого действия. Любая ошибка
    доставки здесь — молчание бота при уже изменённых ценах: админ не знает ни что
    записалось, ни было ли вообще. Поэтому каждый шаг обёрнут, а длинный отчёт режется
    (боевой прайс дал 10 000 символов при лимите 4096, и сообщение просто не ушло).
    """
    parts = _chunks(text)
    try:
        await progress.edit_text(parts[0])
    except Exception:                                   # noqa: BLE001
        logger.exception("Не удалось отредактировать статус — шлём отдельным сообщением")
        try:
            await message.answer(parts[0])
        except Exception:                               # noqa: BLE001
            logger.exception("Отчёт о записи цен не доставлен админу")
    for chunk in parts[1:]:
        try:
            await message.answer(chunk)
        except Exception:                               # noqa: BLE001
            logger.exception("Не доставлена часть отчёта о записи цен")


async def _finish_run(message: Message, store: PricingStore, user_id: int,
                      force: bool = False) -> bool:
    """Завершить прогон: отложенные замечания, итог, выход из режима прайса.

    Один выход на оба пути — и когда последняя марка записана кнопкой, и когда по ней
    нечего было писать. Замечания по прайсу целиком (§9.6) показываются здесь и только
    здесь: повторять их в предложении по каждой марке — шум, админ читает их один раз.

    `force` — для пути кнопки: там прогон мог не начинаться вовсе (однобрендовый прайс),
    но выйти из режима всё равно надо.
    """
    run = await store.get_run(user_id)
    if run is not None and run["remaining"]:
        return False
    if run is None and not force:
        return False

    lines: list[str] = []
    if run and run.get("notes"):
        lines.append("Осталось за рамками разбора:")
        lines += [f"— {n}" for n in run["notes"]]
        lines.append("")
    deferred = await store.list_deferred(user_id)
    if deferred:
        lines.append("Отложено, вернуться позже:")
        lines += [f"— {_deferred_title(t)}" for t in deferred]
        lines.append("Список: /deferred — там же как продолжить.")
        lines.append("")
    doc = (run or {}).get("price_doc") or (run or {}).get("supplier")
    lines.append(f"Прайс «{doc}» обработан полностью." if doc
                 else "Работа с прайсом завершена.")
    lines.append("Пришлите следующий файл, когда понадобится.")

    _files.pop(user_id, None)
    await store.reset(user_id)
    await store.clear_run(user_id)
    await _send(message, "\n".join(lines))
    return True


SHEET_MARK = "=== Лист:"
NOM_MARK = '"not_returned_by_1c"'          # так выглядит только ответ get_1c_nomenclature
DUMP_MIN_CHARS = 4000
DUMP_STUB = ("[Выгрузка листа прайса убрана из истории, чтобы не раздувать контекст. "
             "Файл никуда не делся: нужный кусок перечитай через read_price_file "
             "(параметры sheet / contains / from_row).]")
NOM_STUB = ("[Выгрузка номенклатуры убрана из истории, чтобы не раздувать контекст. "
            "Данные не потеряны: вызови get_1c_nomenclature заново — они берутся из "
            "кэша, к 1С запрос не пойдёт. По возможности сужай выборку параметром "
            "collection_ref.]")
# Сколько марок подряд можно закрыть без участия админа. Защита от цикла: у каждой
# итерации свой запрос к модели.
MAX_AUTO_STEPS = 8


def _dump_kind(text) -> str | None:
    """Какая это тяжёлая выгрузка: лист прайса, номенклатура 1С — или ничего."""
    if not isinstance(text, str) or len(text) <= DUMP_MIN_CHARS:
        return None
    if SHEET_MARK in text:
        return "sheet"
    if NOM_MARK in text:
        return "nomenclature"
    return None


def _is_dump(text) -> bool:
    return _dump_kind(text) is not None


def _dump_texts(content) -> list:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text") for b in content
                if isinstance(b, dict) and b.get("type") == "text"]
    return []


def _prune_file_dumps(messages: list[dict], keep_last: int = 1) -> list[dict]:
    """Старые выгрузки прайса заменяются заглушкой.

    Каждая тяжёлая выгрузка едет в КАЖДЫЙ следующий запрос к модели, а цикл ручной:
    один шаг по коллекции — это несколько запросов со всей историей. На прайсе Артисана
    пять страниц номенклатуры дали 224 000 символов, лист прайса — ещё 89 000.

    Свежая выгрузка каждого вида нужна, все прежние — нет. Прайс лежит в памяти процесса,
    номенклатура — в `_NOM_CACHE`, так что перечитывание не стоит ни запроса к 1С, ни
    запроса в Telegram.
    """
    seen: dict[str, int] = {}
    stubs = {"sheet": DUMP_STUB, "nomenclature": NOM_STUB}
    out = []
    for msg in reversed(messages):
        content = msg.get("content")
        if isinstance(content, list):
            blocks, changed = [], False
            for block in content:
                kind = None
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    kind = next((k for k in (_dump_kind(t)
                                             for t in _dump_texts(block.get("content")))
                                 if k), None)
                if kind:
                    seen[kind] = seen.get(kind, 0) + 1
                    if seen[kind] > keep_last:
                        block = {**block, "content": stubs[kind]}
                        changed = True
                blocks.append(block)
            if changed:
                msg = {**msg, "content": blocks}
        out.append(msg)
    return list(reversed(out))


async def _run(message: Message, user_text: str, orchestrator, onec, store: PricingStore,
               status_msg: Message, user_id: int | None = None) -> None:
    # user_id передаётся явно, когда ход инициирует не админ, а мы сами — после нажатия
    # кнопки (§9.6): там `message` это сообщение БОТА, и from_user в нём — бот, а не админ
    if user_id is None:
        user_id = message.from_user.id

    for _ in range(MAX_AUTO_STEPS):
        tools = PricingTools(onec, store, user_id)
        if user_id in _files:
            tools.set_file(*_files[user_id])
        step_status = status_msg

        async def on_tool(name: str, _inp: dict, _s=step_status) -> None:
            try:
                await _s.edit_text(_STATUS.get(name, f"Выполняю {name}..."))
            except Exception:
                pass

        history = await store.load_messages(user_id)
        history.append({"role": "user", "content": user_text})

        try:
            answer, history = await orchestrator.handle_turn(
                history, on_tool=on_tool, system=PRICING_PROMPT,
                extra_tools=PRICING_TOOLS, extra_executor=tools, base_tools=False)
        except Exception as exc:                       # noqa: BLE001
            logger.exception("Ошибка обработки прайса")
            await step_status.edit_text(describe_api_error(
                exc, "Ошибка при обработке прайса. Подробности в логах."))
            return

        await store.save_messages(user_id, _prune_file_dumps(history))
        try:
            await step_status.delete()
        except Exception:
            pass

        pending = await store.get_pending(user_id)
        markup = (_keyboard(pending.proposal_id, await _in_stage(store, user_id))
                  if pending else None)
        # Предложение админу шлём из last_summary, а не из ответа модели: текст собран
        # кодом, и переписывание его моделью стоило ~2 000 выходных токенов на шаг, а
        # заодно теряло строки. Ответ модели добавляем, только если она сказала что-то
        # своё — вопрос или замечание сверх предложения.
        if pending and tools.last_summary:
            extra = (answer or "").strip()
            text = tools.last_summary
            if extra and extra[:40] not in tools.last_summary:
                text += "\n\n" + extra
        else:
            text = answer
        await _send(message, text, markup)
        if pending is not None:
            return                       # ждём кнопку админа

        # марка закрылась без кнопки (менять нечего) — переходим сами, иначе прогон
        # встанет: кнопки нет, а значит и обработчика, который двинул бы очередь, тоже
        if not tools.advanced_to:
            break
        user_text = (f"По этой марке менять нечего. Продолжай со следующей: "
                     f"{tools.advanced_to}.")
        status_msg = await message.answer(f"Перехожу к {tools.advanced_to}...")

    await _finish_run(message, store, user_id)


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
    await pricing_store.clear_run(message.from_user.id)   # и новый план по маркам
    clear_nomenclature_cache()                        # и свежие цены из 1С

    caption = (message.caption or "").strip()
    task = f"Прислан прайс «{doc.file_name}»." + (f" Комментарий админа: {caption}" if caption else "")
    await _run(message, task, orchestrator, onec, pricing_store, status_msg)


@router.message(Command("cancel_price"))
async def cmd_cancel(message: Message, pricing_store: PricingStore) -> None:
    _files.pop(message.from_user.id, None)
    await pricing_store.reset(message.from_user.id)
    await pricing_store.clear_run(message.from_user.id)
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


def _deferred_title(task: dict) -> str:
    """Кратко: либо ТМ, либо ТМ/коллекция — как просил админ."""
    tm = task.get("tm_name") or task.get("tm_code") or "?"
    return f"{tm} / {task['collection']}" if task.get("collection") else tm


def _deferred_line(i: int, task: dict) -> str:
    doc = task.get("price_doc") or task.get("supplier")
    when = f" от {task['price_date'][:10]}" if (task.get("price_date") or "") else ""
    src = f" — прайс «{doc}»{when}" if doc else ""
    mark = "  ⚠️ устарело, есть прайс новее" if task.get("stale") else ""
    return f"{i}. {_deferred_title(task)}{src}{mark}"


@router.message(Command("deferred"))
async def cmd_deferred(message: Message, pricing_store: PricingStore, is_admin: bool) -> None:
    """Отложенные задачи (§9.7): к чему решили вернуться позже."""
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    tasks = await pricing_store.list_deferred(message.from_user.id)
    if not tasks:
        await message.answer(
            "Отложенных задач нет. Они появляются, когда вы жмёте «Отложить» под "
            "предложением по марке или коллекции.")
        return
    lines = ["Отложенные задачи:"]
    lines += [_deferred_line(i, t) for i, t in enumerate(tasks, 1)]
    lines.append("\nВернуться: /deferred_resume <номер> — прайс поднимется с сервера, "
                 "присылать файл заново не нужно.")
    lines.append("Убрать: /deferred_forget <номер>, /deferred_clear, "
                 "/deferred_clear_stale.")
    await _send(message, "\n".join(lines))


async def _pick(message: Message, command: CommandObject,
                store: PricingStore) -> dict | None:
    tasks = await store.list_deferred(message.from_user.id)
    arg = (command.args or "").strip()
    if not arg.isdigit() or not 1 <= int(arg) <= len(tasks):
        await message.answer("Укажите номер из /deferred, например: /deferred_forget 1")
        return None
    return tasks[int(arg) - 1]


@router.message(Command("deferred_forget"))
async def cmd_deferred_forget(message: Message, command: CommandObject,
                              pricing_store: PricingStore, is_admin: bool) -> None:
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    task = await _pick(message, command, pricing_store)
    if task is None:
        return
    freed = await pricing_store.forget_deferred(message.from_user.id, task["id"])
    price_files.forget(freed)
    await message.answer(f"Убрал из отложенных: {_deferred_title(task)}.")


@router.message(Command("deferred_clear"))
async def cmd_deferred_clear(message: Message, pricing_store: PricingStore,
                             is_admin: bool) -> None:
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    tasks = await pricing_store.list_deferred(message.from_user.id)
    if not tasks:
        await message.answer("Список отложенных и так пуст.")
        return
    price_files.forget(await pricing_store.clear_deferred(message.from_user.id))
    await message.answer(f"Список отложенных очищен ({len(tasks)}). "
                         "Сохранённые прайсы удалены.")


@router.message(Command("deferred_clear_stale"))
async def cmd_deferred_clear_stale(message: Message, pricing_store: PricingStore,
                                   is_admin: bool) -> None:
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    tasks = [t for t in await pricing_store.list_deferred(message.from_user.id) if t["stale"]]
    if not tasks:
        await message.answer("Устаревших отложенных задач нет.")
        return
    price_files.forget(await pricing_store.clear_deferred(message.from_user.id,
                                                          stale_only=True))
    await message.answer("Убрал устаревшие: "
                         + ", ".join(_deferred_title(t) for t in tasks) + ".")


@router.message(Command("deferred_resume"))
async def cmd_deferred_resume(message: Message, command: CommandObject, orchestrator, onec,
                              pricing_store: PricingStore, is_admin: bool) -> None:
    """Вернуться к отложенному: прайс поднимается с диска, пересылать его не нужно."""
    if not is_admin:
        await message.answer("Команда доступна только администратору.")
        return
    if onec is None:
        await message.answer("Интеграция с 1С не настроена.")
        return
    task = await _pick(message, command, pricing_store)
    if task is None:
        return
    content = price_files.load(task.get("file_path"))
    if content is None:
        await message.answer(
            f"Прайс для «{_deferred_title(task)}» на сервере не найден — пришлите файл "
            "заново документом. Саму задачу можно убрать: /deferred_forget.")
        return

    user_id = message.from_user.id
    name = task.get("price_doc") or "прайс"
    _files[user_id] = (name, content)
    await pricing_store.reset(user_id)          # чистый диалог: старой истории здесь нет
    await pricing_store.clear_run(user_id)
    clear_nomenclature_cache()
    status = await message.answer(f"Поднимаю прайс «{name}» с сервера...")
    what = _deferred_title(task)
    await _run(message,
               f"Возвращаемся к отложенному: {what}. Прайс «{name}» тот же. Разбери "
               "только это — план прогона составь из одной этой марки"
               + (f", а внутри неё только коллекция «{task['collection']}»."
                  if task.get("collection") else "."),
               orchestrator, onec, pricing_store, status, user_id=user_id)


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
    parts = _chunks(text)          # длинный прайс даёт сообщение длиннее лимита Telegram
    sent = 0
    for user in await store.list_all():
        if user.telegram_id == admin_id:
            continue
        try:
            for chunk in parts:
                await bot.send_message(user.telegram_id, chunk)
            sent += 1
        except Exception:                          # заблокировал бота, удалил чат и т.п.
            logger.warning("Не доставлено менеджеру %s", user.telegram_id, exc_info=True)
    return sent


@router.callback_query(F.data.startswith("price:"))
async def handle_price_decision(callback: CallbackQuery, onec, pricing_store: PricingStore,
                                store: UserStore, is_admin: bool, orchestrator=None) -> None:
    _, action, raw_id = callback.data.split(":")
    user_id = callback.from_user.id
    if not is_admin:
        await callback.answer("Только администратор", show_alert=True)
        return

    if action in ("skip", "defer", "defer_tm"):
        await _skip_or_defer(callback, action, int(raw_id), onec, pricing_store,
                             orchestrator, user_id)
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
            reply_markup=_keyboard(proposal.proposal_id,
                                   await _in_stage(pricing_store, user_id)))
        return

    await pricing_store.mark_applied(proposal.proposal_id)

    # Цены в 1С уже изменены. Дальше НИЧТО не имеет права оставить админа без ответа:
    # молчание бота после записи неотличимо от «ничего не произошло», а цены при этом
    # стоят новые. Поэтому весь сбор отчёта под общим except с внятным запасным текстом.
    try:
        report = _format_result(result)
        # журнал и рассылка (п.6): 1С хранит дату изменения, но не источник цены —
        # «из какого прайса» знаем только мы, и только отсюда
        if proposal.digest:
            failed = {e.get("ref") for e in (result.get("errors") or []) if e.get("ref")}
            try:
                await pricing_store.record_writes(
                    journal_rows(proposal.digest, _written_on(result), failed))
            except Exception:
                logger.exception("Не удалось записать журнал цен")   # цены уже записаны
            active, _ = resolve(*await pricing_store.load_exclusives())
            sent = await _notify_managers(callback.message.bot, store, proposal.digest,
                                          result, user_id, active)
            if sent:
                report += f"\n\nМенеджерам отправлено уведомление: {sent}."
    except Exception:                                   # noqa: BLE001
        logger.exception("Ошибка при подготовке отчёта после записи цен")
        report = (f"Цены записаны в 1С: обновлено {result.get('updated', 0)} поз. "
                  f"({result.get('date', '')}).\n"
                  "Подробный отчёт собрать не удалось — смотрите логи. "
                  "Записанное можно проверить в 1С и через «когда меняли цены».")

    step = _step_of(proposal)
    # объект дошёл до записи — держать по нему отложенную задачу больше незачем
    price_files.forget(await pricing_store.drop_deferred_for(
        user_id, step["tm_code"] or "", step["collection_ref"]))
    run = await _close_step(pricing_store, user_id, step)
    report += queue_tail(run)
    await _deliver(progress, callback.message, report)

    clear_nomenclature_cache()      # цены в 1С изменились — кэш устарел
    await _continue_or_finish(callback.message, pricing_store, user_id, run,
                              orchestrator, onec, "Цены записаны.")


def _step_of(proposal) -> dict:
    """Какой шаг очереди закрывает это предложение: марка и, возможно, коллекция."""
    groups = (proposal.digest or {}).get("groups") or []
    first = next((g for g in groups if g.get("tm_code")), {})
    return {"tm_code": first.get("tm_code"), "tm_name": first.get("tm_name") or "",
            "collection_ref": first.get("collection_ref") or "",
            "collection": first.get("collection") or ""}


async def _close_step(store: PricingStore, user_id: int, step: dict,
                      whole_tm: bool = False) -> dict | None:
    """Пометить шаг пройденным на том уровне очереди, на котором мы находимся (§9.6.2).

    `whole_tm` закрывает марку вместе с недоработанными коллекциями: админ отложил её
    целиком, и оставшиеся коллекции спрашивать по одной незачем.
    """
    run = await store.get_run(user_id)
    stage = (run or {}).get("stage")
    if whole_tm and stage and stage.get("tm_code") == step["tm_code"]:
        await store.clear_stage(user_id)
    elif stage and stage.get("tm_code") == step["tm_code"] and step["collection_ref"]:
        return await store.mark_collection_done(user_id, step["collection_ref"])
    if step["tm_code"]:
        return await store.mark_tm_done(user_id, step["tm_code"])
    return run


async def _continue_or_finish(message: Message, store: PricingStore, user_id: int,
                              run: dict | None, orchestrator, onec, prefix: str) -> None:
    """Двинуть очередь дальше либо закрыть прогон. Один хвост на все три кнопки."""
    nxt = next_step(run)
    if not nxt:
        await _finish_run(message, store, user_id, force=True)
        return
    # прайс разобран не весь: продолжаем тем же диалогом — файл и история на месте,
    # иначе админу пришлось бы присылать файл заново на каждый шаг (§9.6)
    status = await message.answer(f"Перехожу к {nxt}...")
    await _run(message, f"{prefix} Продолжай: {nxt}.",
               orchestrator, onec, store, status, user_id=user_id)


async def _skip_or_defer(callback: CallbackQuery, action: str, proposal_id: int, onec,
                         store: PricingStore, orchestrator, user_id: int) -> None:
    """«Пропустить» и «Отложить»: цены не пишем, очередь двигаем (§9.7).

    Отличие от «Отмены» — там админ остаётся на шаге, чтобы переспросить агента.
    """
    proposal = await store.take_pending(user_id, proposal_id)
    if proposal is None:
        await callback.answer("Предложение уже обработано или устарело", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    await store.mark_rejected(proposal.proposal_id)
    await callback.message.edit_reply_markup(reply_markup=None)

    step = _step_of(proposal)
    whole_tm = action == "defer_tm"
    if whole_tm:
        # откладываем марку целиком — коллекция в задаче не указывается, иначе вернуться
        # к ней получится только по одной этой коллекции
        step = {**step, "collection_ref": "", "collection": ""}
    title = step["collection"] or step["tm_name"] or step["tm_code"] or "шаг"

    if action == "skip":
        head = f"Пропущено: {title}. Цены не менялись."
    else:
        tools = PricingTools(onec, store, user_id)
        if user_id in _files:
            tools.set_file(*_files[user_id])
        saved = await store.defer_task(user_id, {**step, **(await tools.run_context())})
        what = f"марка {title} целиком" if whole_tm else title
        head = (f"Отложено: {what}. Вернуться — /deferred, файл присылать не нужно."
                if saved else f"«{what}» уже в списке отложенных.")
    await callback.answer()

    run = await _close_step(store, user_id, step, whole_tm=whole_tm)
    await _send(callback.message, head + queue_tail(run))
    await _continue_or_finish(callback.message, store, user_id, run, orchestrator, onec,
                              "Этот шаг пропускаем.")


def _price_label(kind: str) -> str:
    return LABELS.get(kind, kind)


def _format_result(result: dict) -> str:
    """Отчёт по ответу set-prices (§10.3): что записано, что пропущено, ошибки.

    Позиции, записанные по-товарно (форма «б»), сводятся по одинаковому переходу: на
    боевом прайсе их было 43, и построчно отчёт вырастал до 10 000 символов — вдвое
    больше, чем Telegram принимает в одном сообщении.
    """
    lines = [f"Готово, {result.get('date', '')}: обновлено {result.get('updated', 0)} поз."]
    if result.get("unchanged"):
        lines.append(f"Без изменений: {result['unchanged']} поз.")

    per_item: dict[tuple, list[str]] = {}
    for res in result.get("results", []):
        if "changes" in res:
            lines.append(f"• {res.get('tm_name', '')} / {res.get('collection', '')} "
                         f"— {res.get('count')} поз.")
            for ch in res["changes"]:
                mark = " (уже актуально)" if ch.get("skipped") else ""
                old = ch.get("old")
                lines.append(f"    {_price_label(ch['price_type'])}: "
                             f"{'нет' if old is None else old} → {ch.get('new')} "
                             f"— {ch.get('count')} поз.{mark}")
        else:
            for kind, det in (res.get("written") or {}).items():
                key = (kind, det.get("old"), det.get("new"))
                per_item.setdefault(key, []).append(res.get("name") or res.get("ref") or "?")

    if per_item:
        lines.append(f"• по отдельным товарам ({sum(len(v) for v in per_item.values())}):")
        for (kind, old, new), names in per_item.items():
            who = names[0] if len(names) == 1 else f"{len(names)} поз."
            lines.append(f"    {_price_label(kind)}: {'нет' if old is None else old} "
                         f"→ {new} — {who}")

    errors = result.get("errors") or []
    if errors:
        lines.append(f"\nОшибки ({len(errors)}):")
        for e in errors[:20]:
            lines.append(f"  ⚠️ {e.get('code')}: {e.get('message')}")
        if len(errors) > 20:
            lines.append(f"  ... и ещё {len(errors) - 20}")
    return "\n".join(lines)
