"""Agentic-цикл: запрос менеджера → инструменты → ответ для Telegram."""
import logging
from collections.abc import Awaitable, Callable

import anthropic

from src.agent.prompts import SYSTEM_PROMPT
from src.agent.tools import TOOL_DEFINITIONS, ToolExecutor

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5"
MAX_TOKENS = 16000
MAX_ITERATIONS = 30


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
    def __init__(self, api_key: str, executor: ToolExecutor,
                 on_usage: Callable[[dict], Awaitable[None]] | None = None) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._executor = executor
        # Приёмник расхода токенов (§9.6.3). Оркестратор НЕ знает про хранилище: он отдаёт
        # голые счётчики, а метки («чей вызов», поставщик, прайс) подмешивает вызывающий —
        # там, где этот контекст и живёт.
        self._on_usage = on_usage

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
                "model": getattr(response, "model", MODEL),
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
        tools = (list(TOOL_DEFINITIONS) if base_tools else []) + list(extra_tools or [])

        for iteration in range(1, MAX_ITERATIONS + 1):
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
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

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                messages.append({"role": "assistant", "content": content})
                text = "".join(b.text for b in response.content if b.type == "text")
                return (text.strip() or "Не удалось сформировать ответ. Попробуйте ещё раз."), messages

            messages.append({"role": "assistant", "content": content})
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
            messages.append({"role": "user", "content": results})

        logger.error("Превышен лимит итераций (%d)", MAX_ITERATIONS)
        return ("Запрос оказался слишком сложным, не удалось завершить поиск. "
                "Попробуйте уточнить запрос."), messages
