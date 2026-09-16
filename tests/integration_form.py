"""Стенд для формы 1С: живые тестовые данные и ответы на нажатия.

    .venv\\Scripts\\python -m tests.integration_form          загрузить и следить
    .venv\\Scripts\\python -m tests.integration_form --load    только загрузить
    .venv\\Scripts\\python -m tests.integration_form --clean   вычистить зеркало

ЗАЧЕМ ОТДЕЛЬНЫЙ СТЕНД, А НЕ ЗАПУСК БОТА. Бот поднимает Telegram, агента и оплачиваемые
вызовы модели, а проверить надо только форму: видит ли она зеркало, доезжают ли нажатия,
гаснет ли колесико. Стенд изображает агента настолько, насколько это нужно форме, и ни
на шаг больше — выполнение задач тут заглушка, в номенклатуру 1С не пишется ничего.

КАК ЭТО РАБОТАЕТ. Стенд держит снимок в памяти, кладёт его в зеркало и опрашивает очередь
команд. Пришла команда — применяет её к своему снимку и кладёт снимок заново. Поэтому
нажатие в форме даёт видимый результат за те же пять секунд, что и с настоящим агентом.

ПИШЕТ В БОЕВУЮ БАЗУ — но только в зеркало модели и только под номерами 901–903, которых у
модели нет. Номенклатуры, цен и справочников не касается. По Ctrl+C вычищает за собой.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta

from src.config import load_config
from src.onec.client import OnecClient
from src.onec.model_provider import OnecProvider

# Повторы при обрыве сети живут в `OnecClient._retry` — там, где выполняется запрос.
# Второго слоя здесь нет намеренно: при сбое было бы непонятно, чьи это были попытки.
# Стенд нашёл ровно эту дыру — сервер рвёт простаивающее keep-alive соединение, и до
# правки клиента цикл падал на `WinError 10054` через пару минут ожидания.

PERIOD = 3.0          # как часто спрашивать 1С о новых командах
BUSY_USER = "Петров"  # чужой захват: проверка Ф8, все кнопки прайса обязаны погаснуть


def moment(minutes_ago=0):
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def task(tid, kind, order, address, status="к обработке", description="",
         result="", subject="коллекция"):
    return {"id": tid, "kind": kind, "order": order, "subject": subject,
            "address": address, "status": status, "description": description,
            "result": result, "done_at": None}


def fresh_snapshot():
    """Набор, покрывающий всё, что форма умеет показывать.

    Подобран не «чтобы было», а по признакам, которые иначе проверить нечем: чужой захват
    гасит кнопки, устаревший прайс показывает номер свежего, готовый — подсказку о
    закрытии, а пять видов задач должны выстроиться по `Порядок`, а не по алфавиту.
    """
    return [
        {"id": 901, "file": "Прайс Монарх 15.09.2026.xlsx", "supplier": "Монарх Логистик",
         "status": "к обработке", "ready": False,
         "has_newer": False, "newer_id": None,
         "locked_by": "", "locked_until": None, "created_at": moment(40),
         "tasks": [
             # Порядок НАРОЧНО перемешан: если форма сортирует по `Порядок`, в списке
             # они выстроятся 1,2,3,4,5 — а если по идентификатору, останутся как здесь.
             task(9015, "изменение цен", 5, "Монарх Логистик / Vintage",
                  description="В прайсе есть колонка РРЦ, в 1С цены от 12.08.2026."),
             task(9011, "нормализация наименований", 1, "Монарх Логистик / Vintage",
                  description="«Дуб Верона 32кл» и «Дуб верона, 32 класс» — одна позиция."),
             task(9014, "добавление новых", 4, "Монарх Логистик / Adventure",
                  description="Проверить, все ли позиции коллекции заведены в 1С: "
                              "в прайсе 14 артикулов, сверки с номенклатурой не было."),
             task(9012, "изменение свойств", 2, "Монарх Логистик / Adventure",
                  description="В прайсе указан класс износостойкости 32, в 1С пусто."),
             task(9013, "перенос в снятые", 3, "Монарх Логистик / Retro",
                  description="Коллекции нет в новом прайсе, была в июльском."),
         ]},
        {"id": 902, "file": "Most Floor 12.09.2026.xlsx", "supplier": "Мост Флор",
         "status": "частично обработан", "ready": False,
         "has_newer": False, "newer_id": None,
         # ЗАНЯТ ДРУГИМ: форма обязана показать строку захвата и погасить все кнопки
         # этого прайса и его задач (решение Ф8).
         "locked_by": BUSY_USER, "locked_until": moment(-8), "created_at": moment(120),
         "tasks": [
             task(9021, "изменение цен", 5, "Мост Флор / Kronostar",
                  status="выполнена", description="Обновить закупочные.",
                  result="Записано 47 позиций."),
             task(9022, "добавление новых", 4, "Мост Флор / Kronospan",
                  description="12 артикулов не найдены в 1С."),
         ]},
        {"id": 903, "file": "Egger 01.08.2026.xlsx", "supplier": "Эггер",
         "status": "к обработке", "ready": True,
         # УСТАРЕЛ: в строке прайса должен появиться номер свежего.
         "has_newer": True, "newer_id": 901, "locked_by": "", "locked_until": None,
         "created_at": moment(2000),
         "tasks": [
             task(9031, "изменение цен", 5, "Эггер / Pro",
                  status="частично обработана",
                  description="Цены августа.",
                  result="12 из 15 записано, по трём в 1С нет позиций."),
         ]},
    ]


class Stand:
    """Изображает агента ровно настолько, насколько это видно форме."""

    def __init__(self, client):
        self.client = client
        self.prices = fresh_snapshot()
        self.next_task_id = 9100
        self.seen = 0
        # Провайдер держим ради `_parse`: команду формы разбирает РАБОЧИЙ код, а не
        # написанный тут заново. Снимок он отсюда не шлёт — этим занят сам стенд.
        self.parser = OnecProvider(client, None)

    # ------------------------------------------------------------------ обмен

    def push(self):
        answer = self.client.set_model_state(self.prices)
        if answer.get("error"):
            print("  !! 1С отвергла снимок: %s %s"
                  % (answer.get("error"), answer.get("message", "")))
        return answer

    # ------------------------------------------------------------------ поиск

    def price(self, price_id):
        return next((p for p in self.prices if p["id"] == price_id), None)

    def task(self, price_id, task_id):
        price = self.price(price_id)
        if price is None:
            return None
        return next((t for t in price["tasks"] if t["id"] == task_id), None)

    # --------------------------------------------------------------- команды

    def apply(self, raw):
        """Применить команду формы к снимку. Возвращает (получилось, что сказать форме)."""
        kind = str(raw.get("kind") or "")
        price_id = int(raw.get("price_id") or 0)
        task_id = int(raw.get("task_id") or 0)
        payload = raw.get("payload") or {}

        price = self.price(price_id)
        if price is None:
            return False, "прайс №%d не найден" % price_id

        # Правка описания приезжает ВМЕСТЕ с командой, отдельного вида под неё нет (§4.3).
        # Применяется до самой команды: «поправил и тут же нажал Выполнить» должно
        # выполнить ИСПРАВЛЕННОЕ задание, а не прежнее.
        item = self.task(price_id, task_id) if task_id else None
        if item is not None and payload.get("description"):
            item["description"] = payload["description"]

        if kind == "выполнить задачу":
            if item is None:
                return False, "задача №%d не найдена" % task_id
            item["status"] = "выполнена"
            item["result"] = "ЗАГЛУШКА стенда: в 1С ничего не записано."
            item["done_at"] = moment()
            price["ready"] = all(t["status"] in ("выполнена", "частично обработана")
                                 for t in price["tasks"])
            return True, ""

        if kind == "сменить статус задачи":
            if item is None:
                return False, "задача №%d не найдена" % task_id
            status = payload.get("status")
            if not status:
                return False, "в команде нет статуса"
            item["status"] = status
            item["done_at"] = moment() if status != "к обработке" else None
            price["ready"] = all(t["status"] in ("выполнена", "частично обработана")
                                 for t in price["tasks"])
            return True, ""

        if kind == "сменить статус прайса":
            status = payload.get("status")
            if not status:
                return False, "в команде нет статуса"
            price["status"] = status
            return True, ""

        if kind == "удалить задачу":
            before = len(price["tasks"])
            price["tasks"] = [t for t in price["tasks"] if t["id"] != task_id]
            if len(price["tasks"]) == before:
                return False, "задача №%d не найдена" % task_id
            return True, ""

        if kind == "уничтожить прайс":
            self.prices = [p for p in self.prices if p["id"] != price_id]
            return True, ""

        if kind == "пересобрать задачи":
            # Идентификаторы НОВЫЕ, старые не переиспользуются: «выполни задачу 9011»
            # после пересборки должна не найти задачу, а не выполнить другую под тем же
            # номером. И ничего не переносится — ни статусы, ни правки описаний.
            marks = price["tasks"][0]["address"].split(" / ")[0] if price["tasks"] \
                else price["supplier"]
            price["tasks"] = [
                self.new_task("нормализация наименований", 1, marks + " / Vintage",
                              "Пересобрано стендом: наименования."),
                self.new_task("изменение цен", 5, marks + " / Vintage",
                              "Пересобрано стендом: цены."),
            ]
            return True, ""

        return False, "стенд не умеет команду «%s»" % kind

    def new_task(self, kind, order, address, description):
        self.next_task_id += 1
        return task(self.next_task_id, kind, order, address, description=description)


def describe(raw):
    target = ("задача %s" % raw.get("task_id")) if raw.get("task_id") \
        else ("прайс %s" % raw.get("price_id"))
    payload = raw.get("payload") or {}
    extra = ""
    if payload.get("status"):
        extra += " статус=«%s»" % payload["status"]
    if payload.get("description"):
        text = payload["description"]
        extra += " правка описания (%d симв.)" % len(text)
    return "%s · %s · %s%s" % (raw.get("kind"), target, raw.get("actor") or "?", extra)


def watch(stand):
    print("\nСлежу за командами формы. Нажимайте кнопки — буду отвечать как агент.")
    print("Ctrl+C — вычистить зеркало и выйти.\n")

    while True:
        # ОБОРОТ НЕ ИМЕЕТ ПРАВА УБИТЬ СТЕНД. Стенд живёт часами, пока форму правят в
        # конфигураторе, а обновление конфигурации базы данных роняет сервис на
        # несколько секунд: 1С отдаёт страницу веб-сервера «500», и разбор падает.
        # Умереть на этом значит бросить человека без стенда ровно в тот момент, когда
        # он выложил правку и идёт её проверять.
        #
        # Боевой провайдер устроен так же (`OnecProvider.collect` ловит каждый шаг
        # отдельно) — и по той же причине, только у него это ещё и про сеть.
        try:
            turn(stand)
        except KeyboardInterrupt:
            raise
        except Exception as exc:                        # noqa: BLE001
            print("!! оборот сорвался: %s" % exc)
            print("   жду и пробую снова — обновление конфигурации 1С выглядит так же\n")

        time.sleep(PERIOD)


def turn(stand):
    answer = stand.client.agent_commands()

    if answer.get("missing"):
        print("!! в 1С не хватает объектов: %s" % ", ".join(answer["missing"]))
    if answer.get("error"):
        print("!! эндпоинт команд ответил ошибкой: %s" % answer["error"])

    commands = answer.get("commands") or []
    if commands:
        # «принята» — ПЕРВЫМ делом, до применения: форма обязана видеть, что команду
        # забрали, даже если применение затянется или упадёт.
        stand.client.agent_commands_state(
            [{"id": c["id"], "state": "принята", "message": ""}
             for c in commands])

        results = []
        for raw in commands:
            stand.seen += 1
            print("→ %s" % describe(raw))

            # Разбор идёт РАБОЧИМ кодом провайдера, а не отдельным: проверяем то, что
            # поедет в бою, включая нули вместо пустых значений и приставку `1c:`.
            parsed = stand.parser._parse(raw)
            if parsed is None:
                results.append({"id": raw["id"], "state": "отклонена",
                                "message": "неизвестный вид команды"})
                print("   отклонено: провайдер не знает такой вид")
                continue

            ok, reason = stand.apply(raw)
            results.append({"id": raw["id"],
                            "state": "выполнена" if ok else "отклонена",
                            "message": reason})
            print("   %s" % ("применено" if ok else "отклонено: " + reason))

        stand.push()
        stand.client.agent_commands_state(results)
        print("   снимок обновлён, форма покажет через ~5 с\n")


def main():
    parser = argparse.ArgumentParser(description="Стенд формы 1С")
    parser.add_argument("--load", action="store_true",
                        help="только загрузить данные и выйти")
    parser.add_argument("--clean", action="store_true",
                        help="вычистить зеркало и выйти")
    args = parser.parse_args()

    cfg = load_config()
    if not cfg.onec_base_url or not cfg.onec_token:
        print("Нужны ONEC_BASE_URL и ONEC_TOKEN в .env")
        return 2

    client = OnecClient(cfg.onec_base_url, cfg.onec_token, timeout=60)
    stand = Stand(client)

    try:
        if args.clean:
            stand.prices = []
            print("Зеркало вычищено: %s" % json.dumps(stand.push(), ensure_ascii=False))
            return 0

        print("Загружаю тестовые данные: прайсов %d, задач %d"
              % (len(stand.prices), sum(len(p["tasks"]) for p in stand.prices)))
        answer = stand.push()
        if answer.get("error"):
            return 1
        print("1С приняла: %s" % json.dumps(answer, ensure_ascii=False))

        print("""
Что смотреть в форме:
  • №901 Монарх — 5 задач, в списке должны идти по «Порядок»: нормализация,
    свойства, снятые, добавление, цены. НЕ по номеру и не по алфавиту.
  • №902 Most Floor — занят «%s»: строка захвата видна, ВСЕ кнопки прайса
    и его задач погашены (решение Ф8).
  • №903 Egger — помечен устаревшим, в строке номер свежего №901; «Готов»
    показывает подсказку о закрытии.
  • Выделите задачу, поправьте описание и нажмите «Выполнить» — правка должна
    уехать вместе с командой и вернуться в описании задачи.""" % BUSY_USER)

        if args.load:
            print("\nДанные загружены. Слежение не запускаю (--load).")
            return 0

        watch(stand)

    except KeyboardInterrupt:
        print("\n\nВычищаю зеркало…")
        stand.prices = []
        try:
            stand.push()
            print("Готово. Обработано команд: %d" % stand.seen)
        except Exception as exc:                        # noqa: BLE001
            print("Вычистить не удалось (%s). Повторите: "
                  "python -m tests.integration_form --clean" % exc)
        return 0
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
