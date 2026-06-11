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
        messages: list[dict] = [{"role": "user", "content": query}]

        for _ in range(MAX_ITERATIONS):
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )

            if response.stop_reason == "pause_turn":
                # серверный web_search не закончил — продолжаем тем же контекстом
                messages.append({"role": "assistant", "content": response.content})
                continue

            if response.stop_reason == "refusal":
                logger.warning("Модель отклонила запрос: %s", response.stop_details)
                return "Не могу обработать этот запрос. Попробуйте переформулировать."

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                text = "".join(b.text for b in response.content if b.type == "text")
                return text.strip() or "Не удалось сформировать ответ. Попробуйте ещё раз."

            messages.append({"role": "assistant", "content": response.content})
            results = []
            for tool in tool_uses:
                logger.info("Инструмент %s: %s", tool.name, tool.input)
                if on_tool:
                    await on_tool(tool.name, tool.input)
                output = await self._executor.execute(tool.name, tool.input)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool.id,
                        "content": output,
                    }
                )
            messages.append({"role": "user", "content": results})

        logger.error("Превышен лимит итераций (%d)", MAX_ITERATIONS)
        return "Запрос оказался слишком сложным, не удалось завершить поиск. Попробуйте уточнить запрос."
