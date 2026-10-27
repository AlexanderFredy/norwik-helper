"""Agentic-цикл: запрос менеджера → инструменты → ответ для Telegram."""
import logging
from collections.abc import Awaitable, Callable

import anthropic

from src.agent.prompts import SYSTEM_PROMPT
from src.agent.tools import TOOL_DEFINITIONS, ToolExecutor

logger = logging.getLogger(__name__)

# Решение админа 10.10.2026: агент работает на Haiku 5.5. Окно у неё 1 млн, рассуждение
# только адаптивное (бюджетом — 400), новый веб-поиск принимает; сверено со справочником
# моделей API и живыми запросами. Сменить без правки кода — `ANTHROPIC_MODEL` в .env.
MODEL = "claude-haiku-5-5"
MAX_TOKENS = 16000
MAX_ITERATIONS = 30
#: Сколько раз за ход подхватываем ответ, оборванный на `MAX_TOKENS`. Дальше — как раньше:
#: что успело прийти, то и ответ; бесконечно просить «короче» незачем.
MAX_TRUNCATIONS = 2


def _append_to_last_user(messages: list[dict], block: dict) -> None:
    """Дописать блок в последнее сообщение пользователя (строку превращаем в блоки)."""
    last = messages[-1] if messages else None
    if last is None or last.get("role") != "user":
        messages.append({"role": "user", "content": [block]})
        return
    content = last["content"]
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    messages[-1] = {"role": "user", "content": list(content) + [block]}
TRUNCATED_NOTE = (
    "Твой предыдущий ответ оборвался на пределе длины и в него не вошло ничего, кроме "
    "уже выполненных выше вызовов; последний вызов (если был) отброшен — повтори его. "
    "Продолжай КОРОТКО: не рассуждай и не пиши длинного текста, делай работу вызовами "
    "инструментов порциями — не больше 10 вызовов за ответ.")

# Модели ДО 4.6 адаптивного рассуждения не знают: на `thinking: adaptive` они отвечают 400.
# Им рассуждение задаётся бюджетом, а веб-поиск — базовой версией инструмента. Список по
# ПРЕФИКСУ имени: API отдаёт имя с датой («claude-haiku-4-5-20251001»).
_BUDGET_THINKING = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5",
                    "claude-opus-4-1", "claude-sonnet-4-2", "claude-opus-4-2", "claude-3")
THINKING_BUDGET = 4000          # меньше MAX_TOKENS: бюджет входит в предел ответа
_WEB_SEARCH_BASIC = "web_search_20250305"


def legacy_model(model: str) -> bool:
    """Модель из тех, что до 4.6: рассуждение бюджетом, веб-поиск базовый."""
    return str(model or "").startswith(_BUDGET_THINKING)


def thinking_for(model: str) -> dict:
    if legacy_model(model):
        return {"type": "enabled", "budget_tokens": THINKING_BUDGET}
    return {"type": "adaptive"}


def tools_for(model: str, tools: list[dict]) -> list[dict]:
    """Серверный веб-поиск новой версии старым моделям недоступен — подменяем на базовый."""
    if not legacy_model(model):
        return tools
    return [{**t, "type": _WEB_SEARCH_BASIC}
            if str(t.get("type", "")).startswith("web_search_") else t for t in tools]


# На каких блоках можно ставить точку кеширования. thinking и tool_use исключены
# намеренно: API их так не принимает.
_CACHEABLE = {"text", "tool_result", "image", "document"}

# Час, а не пять минут по умолчанию. Между предложением и нажатием кнопки админ читает
# цены и думает — за пять минут префикс протухает, и следующий шаг оплачивается целиком.
# Запись в часовой кеш дороже (×2 против ×1.25), но на прогоне прайса префикс читается
# десятки раз, а чтение стоит ×0.1. Проверено: usage.cache_creation показывает
# ephemeral_1h_input_tokens.
CACHE = {"type": "ephemeral", "ttl": "1h"}


def _cached(messages: list[dict]) -> list[dict]:
    """Копия истории с точкой кеширования на последнем блоке.

    Цикл ручной, поэтому КАЖДЫЙ вызов инструмента — это отдельный запрос со всей историей
    заново. Без кеша шаг по одной коллекции на разобранном прайсе (≈200 тыс. токенов
    контекста, несколько инструментов) стоил миллион входных токенов. Точка кеширования
    делает повторную отправку префикса почти бесплатной.

    Оригинал не трогаем: cache_control не должен попасть в сохраняемую историю — он бы
    копился от хода к ходу и упёрся в лимит точек кеширования.
    """
    if not messages:
        return messages
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list) and content:
        blocks = list(content)
    else:
        return messages
    tail = blocks[-1]
    if not isinstance(tail, dict) or tail.get("type") not in _CACHEABLE:
        return messages
    blocks[-1] = {**tail, "cache_control": CACHE}
    return messages[:-1] + [{**last, "content": blocks}]


class Orchestrator:
    model = MODEL           # на уровне класса — для экземпляров, собранных в обход __init__

    def __init__(self, api_key: str, executor: ToolExecutor,
                 on_usage: Callable[[dict], Awaitable[None]] | None = None,
                 model: str | None = None) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._executor = executor
        self.model = model or MODEL
        logger.info("Модель агента: %s", self.model)
        # Приёмник расхода токенов (§9.6.3). Оркестратор НЕ знает про хранилище: он отдаёт
        # голые счётчики, а метки («чей вызов», поставщик, прайс) подмешивает вызывающий —
        # там, где этот контекст и живёт.
        self._on_usage = on_usage

    @property
    def executor(self) -> ToolExecutor:
        """Исполнитель инструментов. Наружу он нужен ровно затем, чтобы забрать готовый
        ответ, который инструмент отправляет МИМО модели (`take_pending`)."""
        return self._executor

    async def _record_usage(self, response, labels: dict | None, iteration: int) -> None:
        """Снять расход одного вызова. Сбой учёта не имеет права трогать прогон.

        Прайсовый прогон идёт по живой 1С и стоит дорого; потерять его из-за сбоя журнала
        было бы обменом ценного на бесплатное.
        """
        if self._on_usage is None:
            return
        try:
            usage = response.usage
            await self._on_usage({
                **(labels or {}),
                "model": getattr(response, "model", self.model),
                "iteration": iteration,
                "tools": ",".join(b.name for b in response.content
                                  if b.type == "tool_use") or None,
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
                "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            })
        except Exception:                                  # noqa: BLE001
            logger.warning("Не удалось записать расход токенов", exc_info=True)

    async def handle_query(
        self,
        query: str,
        on_tool: Callable[[str, dict], Awaitable[None]] | None = None,
        system: str | None = None,
        usage_labels: dict | None = None,
    ) -> str:
        """Обрабатывает один запрос менеджера и возвращает текст ответа."""
        text, _ = await self.handle_turn([{"role": "user", "content": query}],
                                         on_tool=on_tool, system=system,
                                         usage_labels=usage_labels)
        return text

    async def handle_turn(
        self,
        messages: list[dict],
        on_tool: Callable[[str, dict], Awaitable[None]] | None = None,
        system: str | None = None,
        extra_tools: list[dict] | None = None,
        extra_executor=None,
        base_tools: bool = True,
        usage_labels: dict | None = None,
    ) -> tuple[str, list[dict]]:
        """Ход диалога поверх истории. Возвращает (ответ, обновлённая история).

        `extra_tools`/`extra_executor` подключают режимные инструменты (напр. обновление
        цен). `base_tools=False` убирает менеджерские: в режиме цен они не нужны, но
        уезжают в каждый запрос и вдобавок соблазняют модель полезть в почту.
        История сериализуема — её сохраняет вызывающий (§12.1 спеки).
        """
        messages = list(messages)
        tools = tools_for(self.model, (list(TOOL_DEFINITIONS) if base_tools else [])
                          + list(extra_tools or []))
        truncations = 0

        for iteration in range(1, MAX_ITERATIONS + 1):
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                thinking=thinking_for(self.model),
                system=[
                    {
                        "type": "text",
                        "text": system or SYSTEM_PROMPT,
                        "cache_control": CACHE,
                    }
                ],
                tools=tools,
                messages=_cached(messages),
            )

            # Расход снимаем ДО любых ветвлений: ниже есть и `continue` (pause_turn), и
            # ранний `return` (refusal), и оба уже оплачены. Учёт только на успешном пути
            # давал бы тихий и систематический недосчёт.
            await self._record_usage(response, usage_labels, iteration)

            content = [b.model_dump() for b in response.content]   # для персистентности

            if response.stop_reason == "pause_turn":
                # серверный web_search не закончил — продолжаем тем же контекстом
                messages.append({"role": "assistant", "content": content})
                continue

            if response.stop_reason == "refusal":
                logger.warning("Модель отклонила запрос: %s", response.stop_details)
                return "Не могу обработать этот запрос. Попробуйте переформулировать.", messages

            if response.stop_reason == "max_tokens" and truncations < MAX_TRUNCATIONS:
                # ОБРЫВ ПО ДЛИНЕ — НЕ ОКОНЧАТЕЛЬНЫЙ ОТВЕТ (бой 26.10.2026, сборка Артисаны:
                # 16 000 выходных токенов без единого `add_task`, и цикл принял обрубок за
                # итог — задач не завелось ни одной). Целые вызовы выполняем, последний
                # (он мог оборваться посреди входа) отбрасываем, и просим продолжить короче.
                truncations += 1
                logger.warning("Ответ модели оборвался на пределе %d токенов (%d-й раз)",
                               MAX_TOKENS, truncations)
                blocks = list(response.content)
                last_call = max((i for i, b in enumerate(blocks) if b.type == "tool_use"),
                                default=None)
                if last_call is not None:
                    # Реплика ДО последнего вызова — целиком, с рассуждением: при вызовах
                    # инструментов API требует вернуть блоки thinking той же реплики.
                    head = blocks[:last_call]
                else:
                    # Вызовов нет — оборвалось рассуждение или текст. Рассуждение без
                    # подписи вернуть нельзя, текст — можно и полезно: модель увидит, где
                    # остановилась.
                    head = [b for b in blocks if b.type == "text"]
                calls = [b for b in head if b.type == "tool_use"]
                note = {"type": "text", "text": TRUNCATED_NOTE}
                if head:
                    messages.append({"role": "assistant",
                                     "content": [b.model_dump() for b in head]})
                    results = await self._run_tools(calls, on_tool, extra_executor)
                    messages.append({"role": "user", "content": results + [note]})
                else:
                    # Ответить нечем — дописываем пояснение в прошлое сообщение
                    # пользователя: две реплики пользователя подряд API не примет как есть.
                    _append_to_last_user(messages, note)
                continue

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                messages.append({"role": "assistant", "content": content})
                text = "".join(b.text for b in response.content if b.type == "text")
                return (text.strip() or "Не удалось сформировать ответ. Попробуйте ещё раз."), messages

            messages.append({"role": "assistant", "content": content})
            results = await self._run_tools(tool_uses, on_tool, extra_executor)
            messages.append({"role": "user", "content": results})

        logger.error("Превышен лимит итераций (%d)", MAX_ITERATIONS)
        return ("Запрос оказался слишком сложным, не удалось завершить поиск. "
                "Попробуйте уточнить запрос."), messages

    async def _run_tools(self, tool_uses, on_tool, extra_executor) -> list[dict]:
        """Выполнить вызовы инструментов одного ответа модели → блоки `tool_result`."""
        results = []
        for tool in tool_uses:
            logger.info("Инструмент %s: %s", tool.name, tool.input)
            if on_tool:
                await on_tool(tool.name, tool.input)
            if extra_executor is not None and extra_executor.handles(tool.name):
                output = await extra_executor.execute(tool.name, tool.input)
            else:
                output = await self._executor.execute(tool.name, tool.input)
            results.append(
                {
                    # output — строка ЛИБО список блоков: read_price_file прикладывает
                    # к тексту баннеры из прайса картинками, прочитать их может только
                    # модель. Не сводить к str().
                    "type": "tool_result",
                    "tool_use_id": tool.id,
                    "content": output,
                }
            )
        return results
