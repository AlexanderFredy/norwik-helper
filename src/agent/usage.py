"""Стоимость обращений к API: тарифы, арифметика, показ админу (§9.6.3).

Считаем ЗДЕСЬ, а не при записи в журнал. В базе лежат только счётчики токенов: тариф
может смениться, и пересчёт задним числом исказил бы историю, если бы доллары были
записаны вместе со строкой.

Главная тонкость — кеш. Бот пишет в ЧАСОВОЙ кеш (`CACHE` в orchestrator.py), а не в
пятиминутный по умолчанию: запись туда стоит вдвое против обычного входа, зато чтение
идёт по десятой доле. На прайсовом прогоне префикс читается десятки раз, поэтому
соотношение «прочитано из кеша / оплачено полностью» — главное число во всём отчёте.
"""
from __future__ import annotations

# $ за миллион токенов: (вход, выход). Снимок тарифов на сентябрь 2026 — при их изменении
# правится только эта таблица, журнал пересчитается сам.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}

# Множители к ВХОДНОМУ тарифу.
CACHE_READ = 0.1        # чтение из кеша — десятая доля
CACHE_WRITE_1H = 2.0    # запись в часовой кеш; пятиминутная стоила бы 1.25

_MILLION = 1_000_000


def cost(model: str, input_tokens: int = 0, output_tokens: int = 0,
         cache_read: int = 0, cache_write: int = 0) -> float | None:
    """Стоимость одного вызова в долларах. None — тариф модели неизвестен.

    Неизвестную модель НЕ приводим к ближайшей: молча заниженный или завышенный счёт хуже
    честного пробела, ради которого и затевался учёт.
    """
    rates = PRICES.get(model)
    if rates is None:
        return None
    price_in, price_out = rates
    return (
        input_tokens * price_in
        + cache_read * price_in * CACHE_READ
        + cache_write * price_in * CACHE_WRITE_1H
        + output_tokens * price_out
    ) / _MILLION


def total_cost(rows) -> tuple[float, set[str]]:
    """Сумма по строкам, сгруппированным ПО МОДЕЛИ, и модели с неизвестным тарифом.

    Группировка по модели обязательна: за период их может быть несколько, и один общий
    множитель дал бы неверный итог.
    """
    total, unknown = 0.0, set()
    for row in rows:
        amount = cost(row.get("model", ""),
                      row.get("input_tokens", 0), row.get("output_tokens", 0),
                      row.get("cache_read", 0), row.get("cache_write", 0))
        if amount is None:
            unknown.add(row.get("model") or "?")
        else:
            total += amount
    return total, unknown


def money(amount: float) -> str:
    """Доллары. Мелкие суммы не округляем в ноль — на шаге прогона они и есть предмет."""
    if amount and amount < 0.01:
        return f"${amount:.4f}"
    return f"${amount:,.2f}".replace(",", " ")


def number(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


def summarize(rows) -> dict:
    """Свернуть строки (по моделям) в один набор чисел для показа."""
    keys = ("calls", "input_tokens", "output_tokens", "cache_read", "cache_write")
    out = {k: sum(int(r.get(k, 0) or 0) for r in rows) for k in keys}
    out["amount"], out["unknown"] = total_cost(rows)
    served = out["cache_read"] + out["input_tokens"] + out["cache_write"]
    # Доля префикса, отданного из кеша. Близкая к нулю на прайсовом прогоне означает, что
    # точка кеширования не работает, — это дороже любых настроек effort.
    out["cache_share"] = round(100 * out["cache_read"] / served) if served else 0
    return out


def render_block(title: str, rows) -> str:
    """Один раздел отчёта `/tokens`."""
    s = summarize(rows)
    if not s["calls"]:
        return f"{title}: обращений не было."
    tail = f" (тариф неизвестен: {', '.join(sorted(s['unknown']))})" if s["unknown"] else ""
    return (
        f"{title}: {money(s['amount'])}{tail}\n"
        f"  вызовов {s['calls']}, из кеша {s['cache_share']}%\n"
        f"  вход {number(s['input_tokens'])} · кеш-чтение {number(s['cache_read'])} · "
        f"кеш-запись {number(s['cache_write'])} · выход {number(s['output_tokens'])}"
    )
