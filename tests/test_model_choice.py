"""Модель агента выбирается переменной окружения (просьба админа 10.10.2026: сравнить с Haiku).

Модели до 4.6 не знают адаптивного рассуждения — на `thinking: adaptive` отвечают 400 — и
новой версии веб-поиска. Проверено живым запросом к claude-haiku-4-5-20251001.
"""
import os
import unittest
from unittest import mock

from src.agent import orchestrator as orch
from src.agent.usage import cost
from src.config import load_config

HAIKU = "claude-haiku-4-5-20251001"
ENV = {"TELEGRAM_BOT_TOKEN": "t", "ADMIN_TELEGRAM_ID": "1", "MAIL_USER": "u",
       "MAIL_PASSWORD": "p", "ANTHROPIC_API_KEY": "k"}


class ThinkingTest(unittest.TestCase):

    def test_haiku_gets_a_budget(self):
        self.assertEqual(orch.thinking_for(HAIKU),
                         {"type": "enabled", "budget_tokens": orch.THINKING_BUDGET})
        self.assertLess(orch.THINKING_BUDGET, orch.MAX_TOKENS)

    def test_current_models_stay_adaptive(self):
        # Haiku 5.5 бюджет рассуждения ОТВЕРГАЕТ (400: «thinking.type.enabled is not
        # supported») — путать её с Haiku 4.5 нельзя, проверено живым запросом.
        for model in ("claude-haiku-5-5", "claude-opus-5", "claude-opus-5-5",
                      "claude-sonnet-5-5", "claude-fable-5-1"):
            self.assertEqual(orch.thinking_for(model), {"type": "adaptive"}, model)


class ToolsTest(unittest.TestCase):

    TOOLS = [{"type": "web_search_20260209", "name": "web_search"},
             {"name": "read_price", "input_schema": {"type": "object"}}]

    def test_haiku_gets_the_basic_web_search(self):
        out = orch.tools_for(HAIKU, self.TOOLS)
        self.assertEqual(out[0]["type"], "web_search_20250305")
        self.assertEqual(out[0]["name"], "web_search")
        self.assertEqual(out[1], self.TOOLS[1], "свои инструменты не трогаем")

    def test_current_models_keep_the_new_one(self):
        self.assertIs(orch.tools_for("claude-opus-5", self.TOOLS), self.TOOLS)


class ConfigTest(unittest.TestCase):

    def test_default_model_is_haiku_5_5(self):
        """Решение админа 10.10.2026."""
        with mock.patch.dict(os.environ, ENV, clear=True):
            self.assertEqual(load_config().anthropic_model, "claude-haiku-5-5")
        self.assertEqual(orch.MODEL, "claude-haiku-5-5")

    def test_model_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {**ENV, "ANTHROPIC_MODEL": f" {HAIKU} "}, clear=True):
            self.assertEqual(load_config().anthropic_model, HAIKU)


class CostTest(unittest.TestCase):

    def test_dated_model_name_is_priced(self):
        """API отдаёт имя с датой — тариф обязан находиться, иначе сравнивать нечего."""
        self.assertAlmostEqual(cost(HAIKU, input_tokens=1_000_000), 1.0)
        self.assertAlmostEqual(cost(HAIKU, output_tokens=1_000_000), 5.0)

    def test_haiku_5_5_short_prompt(self):
        """Тариф админа 10.10.2026: до 100 тыс. токенов промпта — $0.10 / $0.50."""
        self.assertAlmostEqual(cost("claude-haiku-5-5", input_tokens=50_000,
                                    output_tokens=1_000), 0.0055)

    def test_haiku_5_5_long_prompt_is_priced_whole_by_the_upper_tier(self):
        """Свыше 100 тыс. — $0.50 / $2.50 за ВЕСЬ запрос. Кеш входит в длину промпта: на
        прогоне по прайсу почти весь вход — чтение из кеша."""
        got = cost("claude-haiku-5-5", input_tokens=2_000, cache_read=150_000,
                   output_tokens=1_000)
        expected = (2_000 * 0.50 + 150_000 * 0.50 * 0.1 + 1_000 * 2.50) / 1_000_000
        self.assertAlmostEqual(got, expected)

    def test_the_edge_itself_is_the_lower_tier(self):
        self.assertAlmostEqual(cost("claude-haiku-5-5", input_tokens=100_000), 0.01)

    def test_unknown_model_is_still_unknown(self):
        self.assertIsNone(cost("claude-mystery-9"))


if __name__ == "__main__":
    unittest.main()
