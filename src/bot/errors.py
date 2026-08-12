"""Понятное объяснение сбоя API вместо «подробности в логах».

Часть сбоев чинится не кодом, а действием владельца: пополнить счёт, заменить ключ,
подождать. Прятать такое в лог — значит заставлять админа лезть в консоль за тем, что
бот и так знает.
"""
from __future__ import annotations

GENERIC = "Ошибка при обработке запроса. Подробности в логах."

_BILLING = ("Закончились средства на счёте Anthropic API — пополните баланс "
            "(Plans & Billing) и повторите. Прайс не потерян: пришлите файл заново.")


def describe_api_error(exc: Exception, generic: str = GENERIC) -> str:
    """Текст для админа по исключению Anthropic SDK."""
    try:
        import anthropic
    except ImportError:                                  # тестовое окружение без SDK
        return generic

    text = str(exc).lower()

    if isinstance(exc, anthropic.BadRequestError):
        # биллинг приходит именно 400, а не 402 — отличаем по тексту
        if "credit balance" in text or "billing" in text:
            return _BILLING
        return generic                                   # прочие 400 — это наш баг
    if isinstance(exc, anthropic.AuthenticationError):
        return ("Ключ ANTHROPIC_API_KEY не принят — проверьте его в .env "
                "и перезапустите бота.")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "Доступ к модели запрещён для этого ключа Anthropic API."
    if isinstance(exc, anthropic.RateLimitError):
        return "Превышен лимит запросов к Anthropic API — повторите через минуту."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Нет связи с Anthropic API — проверьте интернет и повторите."
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code in (500, 502, 503, 529):
        return "Anthropic API временно недоступен — повторите через несколько минут."
    return generic
