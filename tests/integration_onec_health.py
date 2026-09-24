"""Проверка здоровья HTTP-сервиса 1С: сколько запросов проваливается в никуда.

    python -m tests.integration_onec_health

Не юнит-тест: ходит в боевую 1С, читает и ничего не пишет. Нужен .env.

ЗАЧЕМ. 24.09.2026 агент по кругу упирался в таймауты, и пять версий подряд оказались
неверными — все пять искали причину в 1С. Нашлась она у нас: ПЕРЕИСПОЛЬЗУЕМОЕ СОЕДИНЕНИЕ.
По одному и тому же сокету лёгкий `selling-tm` (норма 0,2 с) не отвечал КАЖДЫЙ ТРЕТИЙ раз
с точностью до номера (2, 5, 8, 11…), выгрузка позиции — каждый тринадцатый; новым
соединением на запрос проходили 9 из 9. Сервер закрывает keep-alive молча, и наш запрос
уходит в мёртвый сокет. После `max_keepalive_connections=0` — 0 провалов из 30 в обеих
сериях.

Проверка меряет ДОЛЮ ПРОВАЛОВ, а не время ответа: среднее здесь ничего не показывает —
здоровые ответы быстрые, а больные не приходят никогда. Прогонять после любой правки в
транспорте клиента и всякий раз, когда «1С тормозит»: разница между «медленно» и «не
отвечает» видна только так.
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
            print("  Первым делом — ПЕРЕИСПОЛЬЗОВАНИЕ СОЕДИНЕНИЯ: именно оно теряло "
                  "запросы 24.09.2026 (`max_keepalive_connections=0` в OnecClient).")
            print("  Если там ноль, а провалы есть — смотреть на стороне публикации: "
                  "рабочие процессы пула IIS, зависшие соединения с ИБ.")
        else:
            print("  Провалов нет: сервис отвечает на каждый запрос.")
    finally:
        onec.close()


if __name__ == "__main__":
    main()
