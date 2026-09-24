"""Проверка здоровья HTTP-сервиса 1С: сколько запросов проваливается в никуда.

    python -m tests.integration_onec_health

Не юнит-тест: ходит в боевую 1С, читает и ничего не пишет. Нужен .env.

ЗАЧЕМ. 24.09.2026 агент по кругу упирался в таймауты, и версии «больная карточка»,
«тяжёлая марка», «маленькая страница» не подтвердились ни одна. Подтвердилось другое:
СЕРВИС ТЕРЯЕТ ЗАПРОСЫ. Тридцать одинаковых вызовов подряд по одной и той же позиции —
двенадцать отвечают за 1,3 с, тринадцатый не отвечает вовсе; тридцать вызовов самого
лёгкого эндпоинта — каждый третий (№2, №5, №8, №11) молчит по двадцать пять секунд при
норме в полсекунды. Карточка до и после провала отдаётся за ту же секунду.

Поэтому проверка меряет ДОЛЮ ПРОВАЛОВ, а не время ответа: среднее здесь ничего не
показывает — здоровые ответы быстрые, а больные не приходят никогда.

Ноль провалов — сервис здоров. Иначе чинить надо на стороне публикации, а не в агенте:
у него на такой случай есть только терпение (`by_tm_all` разбирает страницу по одной и
повторяет однажды), и оно стоит минут.
"""
from __future__ import annotations

import time

from src.config import load_config
from src.onec.client import OnecClient

#: Сколько раз стучимся. Тридцати хватает: провалы идут с ровным шагом, и при доле 1/13
#: их видно уже на втором десятке.
TIMES = 30

#: Столько ждём ответа. Здоровый лёгкий вызов укладывается в секунду, тяжёлая страница —
#: в три; всё, что дольше десяти, уже не «медленно», а «не ответит».
PATIENCE = 10.0


def series(label: str, call, times: int = TIMES) -> list[int]:
    print(f"\n=== {label}: {times} раз подряд")
    lost = []
    for n in range(1, times + 1):
        started = time.monotonic()
        try:
            call()
            spent = time.monotonic() - started
            print(f"  №{n:>2} {spent:5.1f} c")
        except Exception as exc:                        # noqa: BLE001
            print(f"  №{n:>2} {time.monotonic() - started:5.1f} c  ПРОВАЛ "
                  f"{type(exc).__name__}")
            lost.append(n)
    share = f"{len(lost)}/{times}"
    print(f"  провалов: {share}" + (f", на запросах {lost}" if lost else " — сервис здоров"))
    return lost


def main() -> None:
    config = load_config()
    if not (config.onec_base_url and config.onec_token):
        print("1С не настроена в .env — проверять нечего")
        return

    onec = OnecClient(config.onec_base_url, config.onec_token,
                      timeout=PATIENCE, retries=1)
    try:
        marks = onec.selling_tm()
        tm = marks[0].code if marks else ""
        light = series("лёгкий вызов selling-tm", lambda: onec.selling_tm())
        heavy = []
        if tm:
            heavy = series(f"одна позиция марки {tm} (by-tm)",
                           lambda: onec.by_tm(tm, page=1, size=1))

        print("\n--- итог")
        if light or heavy:
            print("  СЕРВИС ТЕРЯЕТ ЗАПРОСЫ. Это не агент и не данные: провалившийся вызов "
                  "тут же проходит повторно.")
            print("  Смотреть на стороне публикации — пул приложений IIS (рабочих "
                  "процессов должно быть РОВНО ОДИН), зависшие соединения с ИБ, "
                  "повторное использование сеанса.")
        else:
            print("  Провалов нет: сервис отвечает на каждый запрос.")
    finally:
        onec.close()


if __name__ == "__main__":
    main()
