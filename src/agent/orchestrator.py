"""Agentic-цикл: запрос менеджера → инструменты → ответ для Telegram."""
import logging
from collections.abc import Awaitable, Callable

import anthropic

from src.agent.prompts import SYSTEM_PROMPT
from src.agent.tools import TOOL_DEFINITIONS, ToolExecutor

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"
MAX_TOKENS = 16000
MAX_ITERATIONS = 30


class Orchestrator:
    def __init__(self, api_key: str, executor: ToolExecutor) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._executor = executor

    async def handle_query(
        self,
        query: str,
        on_tool: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> str:
        """Обрабатывает один запрос менеджера и возвращает текст ответа."""
        text, _ = await self.handle_turn([{"role": "user", "content": query}], on_tool=on_tool)
        return text

    async def handle_turn(
        self,
        messages: list[dict],
        on_tool: Callable[[str, dict], Awaitable[None]] | None = None,
        system: str | None = None,
        extra_tools: list[dict] | None = None,
        extra_executor=None,
    ) -> tuple[str, list[dict]]:
        """Ход диалога поверх истории. Возвращает (ответ, обновлённая история).

        `extra_tools`/`extra_executor` подключают режимные инструменты (напр. обновление
        цен) поверх базовых. История сериализуема — её сохраняет вызывающий (§12.1 спеки).
        """
        messages = list(messages)
        tools = list(TOOL_DEFINITIONS) + list(extra_tools or [])

        for _ in range(MAX_ITERATIONS):
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                system=[
                    {
                        "type": "text",
                        "text": system or SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=tools,
                messages=messages,
            )

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
