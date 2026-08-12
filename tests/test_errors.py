"""Сбой API объясняется админу словами, а не «подробности в логах»."""
import unittest

import anthropic
import httpx

from src.bot.errors import GENERIC, describe_api_error


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com"))


def _api_error(cls, status: int, message: str):
    return cls(message, response=_response(status), body=None)


class DescribeApiErrorTest(unittest.TestCase):
    def test_billing_is_named_explicitly(self):
        """Реальный ответ API при нулевом балансе — 400, а не 402."""
        exc = _api_error(anthropic.BadRequestError, 400,
                         "Your credit balance is too low to access the Anthropic API. "
                         "Please go to Plans & Billing to upgrade or purchase credits.")
        text = describe_api_error(exc)
        self.assertIn("Закончились средства", text)
        self.assertIn("пришлите файл заново", text)

    def test_other_400_stays_generic(self):
        """Прочие 400 — это наша ошибка запроса, админу от текста API толку нет."""
        exc = _api_error(anthropic.BadRequestError, 400, "messages.1: unexpected role")
        self.assertEqual(describe_api_error(exc), GENERIC)

    def test_bad_key(self):
        exc = _api_error(anthropic.AuthenticationError, 401, "invalid x-api-key")
        self.assertIn("ANTHROPIC_API_KEY", describe_api_error(exc))

    def test_rate_limit(self):
        exc = _api_error(anthropic.RateLimitError, 429, "rate limited")
        self.assertIn("лимит запросов", describe_api_error(exc))

    def test_overloaded(self):
        exc = _api_error(anthropic.APIStatusError, 529, "overloaded")
        self.assertIn("временно недоступен", describe_api_error(exc))

    def test_connection(self):
        exc = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
        self.assertIn("Нет связи", describe_api_error(exc))

    def test_unknown_error_uses_caller_text(self):
        self.assertEqual(describe_api_error(ValueError("boom"), "своё сообщение"),
                         "своё сообщение")


if __name__ == "__main__":
    unittest.main()
