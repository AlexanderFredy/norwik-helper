"""Что из контракта `by-tm` реально выложено в 1С.

    python -m tests.integration_bytm_contract

ЗАЧЕМ. Файл `specs/1c/by-tm.bsl` кладётся в модуль руками, и правки в нём лежат в РАЗНЫХ
функциях: точка входа, сборка запроса, сборка позиции. Дважды подряд (29 и 30.09.2026)
выкладывалась ЧАСТЬ: guard на параметры уезжал, а поля в ответе — нет. Снаружи это выглядит
не как ошибка, а как странные данные: поле пустое, отбор «не работает», выборка в одну
позицию. Каждый раз это стоило получаса разбирательств.

Проверка спрашивает боевую 1С по одному признаку на правку и говорит про каждую: выложена
или нет. Запускать ПОСЛЕ каждой выкладки by-tm.bsl.

Нужен .env с ONEC_BASE_URL и ONEC_TOKEN. Ничего не пишет — только читает.
"""
import json
import sys

from src.agent.tools import _months_ago
from src.config import load_config
from src.onec.client import OnecClient

FUTURE = "2030-01-01"


def raw_item(onec: OnecClient, **params) -> dict:
    """Одна позиция СЫРЫМ ответом: нужно отличить «ключа нет» от «ключ пуст»."""
    r = onec._get("/get-products/by-tm", params={"page": 1, "size": 1, **params})
    data = json.loads(r.content.decode("utf-8-sig"))
    items = data.get("items") or []
    return items[0] if items else {}


def main() -> int:
    config = load_config()
    onec = OnecClient(config.onec_base_url, config.onec_token, timeout=90)
    since = _months_ago(3)
    mark = onec.selling_tm()[0]
    checks: list[tuple[str, bool, str]] = []

    # 1. Поле даты создания в ответе.
    item = raw_item(onec, tm=mark.code)
    checks.append(("поле `created` в позиции", "created" in item,
                   "ветка \"ДатаСоздания\" в функции Безопасно + функция ДатаСозданияISO"))

    # 2. Отбор по дате. Заведомо будущая граница обязана отсечь всё.
    future = onec.by_tm(mark.code, page=1, size=1, created_from=FUTURE)
    checks.append(("отбор `created_from` применяется", future.total == 0,
                   "блок created_from в ПолучитьТоварыПоПроизводителю"))

    # 3. Защита: без марки и без даты — отказ.
    guard = onec.by_tm(None, page=1, size=1)
    checks.append(("без tm и без created_from — отказ",
                   any(e.get("code") == "tm_missing" for e in guard.errors),
                   "блок КодПроизводителя = Неопределено в точке входа"))

    # 4. Запрос без марки идёт по всем выгружаемым маркам. Одна позиция в ответе означает,
    #    что отбор остался равенством с ПУСТОЙ маркой: подстановка %%ОтборПоМарке%% не
    #    выложена.
    everyone = onec.by_tm(None, page=1, size=8, created_from=since)
    by_mark = onec.by_tm(mark.code, page=1, size=1, created_from=since)
    checks.append((f"запрос без tm отбирает по выгружаемым маркам "
                   f"(нашёл {everyone.total}, у одной марки {by_mark.total})",
                   everyone.total > by_mark.total,
                   "подстановка %%ОтборПоМарке%% в ПолучитьТоварыПоПроизводителю"))

    # 5. Марка в самой позиции — без неё группировать ответ по ТМ не по чему.
    fresh = raw_item(onec, created_from=since)
    checks.append(("поля `tm` и `tm_code` в позиции",
                   "tm" in fresh and "tm_code" in fresh,
                   "две строки Вставить(\"tm\"…) в функции ДанныеТовара"))

    print(f"Проверка контракта by-tm на {config.onec_base_url}\n")
    bad = 0
    for name, ok, where in checks:
        print(f"  [{'OK ' if ok else 'НЕТ'}] {name}")
        if not ok:
            bad += 1
            print(f"        не выложено: {where}")

    print()
    if bad:
        print(f"НЕ ВЫЛОЖЕНО ЧАСТЕЙ: {bad}. Выложите specs/1c/by-tm.bsl целиком "
              "и запустите проверку снова.")
        return 1
    print("Выложено всё.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
