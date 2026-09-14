"""Живая проверка обмена с формой 1С (specs/1c-model-form.md §3).

    .venv\\Scripts\\python -m tests.integration_model_state

Требует `ONEC_BASE_URL` и `ONEC_TOKEN` в `.env`; ни то, ни другое не печатается.

ЧТО ЭТО ПРОВЕРЯЕТ, ЧЕГО НЕ ПРОВЕРЯТ ЮНИТ-ТЕСТЫ. Весь код обмена живёт в BSL, на стороне
1С, и здесь его единственная точка касания — HTTP. Подменять его заглушкой бессмысленно:
ошибки, которые он даёт, — это «нет такого регистра», «поле недоступно для записи» и
«тело не объект», и ловятся они только живой базой.

ПИШЕТ В БОЕВУЮ БАЗУ, но только в зеркало модели (`ии_МодельПрайсы`, `ии_МодельЗадачи`) и
только под номерами 901/902, которых у модели нет. Последним шагом зеркало вычищается
пустым снимком. Номенклатуры и цен не касается вовсе.

ГЛАВНАЯ ПРОВЕРКА — ШАГ 4: повтор ТОГО ЖЕ снимка обязан дать `changed = 0` и не тронуть
версию. Если разница считается неверно, счётчик растёт на каждом обороте, форма
перечитывает списки каждые пять секунд и выделение прыгает у админа под руками — то
самое, ради чего разница и делалась (§2.5 спеки).
"""
import json
import time

import httpx

from src.config import load_config

JSON_UTF8 = {"Content-Type": "application/json; charset=utf-8"}

# Сервис 1С периодически не принимает соединение (WinError 10060) — в `OnecClient` под это
# есть повторы. Здесь они нужны тем более: обрыв на середине оставил бы тестовые строки в
# зеркале, потому что до шага уборки дело просто не дошло бы.
NET = (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout,
       httpx.RemoteProtocolError)

SNAP = {"prices": [
    {"id": 901, "file": "ПРОВЕРКА Монарх 14.09.2026.xlsx", "supplier": "ПРОВЕРКА Монарх",
     "status": "к обработке", "ready": False, "has_newer": False, "newer_id": None,
     "locked_by": "", "locked_until": None, "created_at": "2026-09-14T11:33:00",
     "tasks": [
         {"id": 9011, "kind": "изменение цен", "order": 5, "subject": "коллекция",
          "address": "ПРОВЕРКА Монарх / Vintage", "status": "к обработке",
          "description": "проверочная задача, в 1С ничего не пишет",
          "result": "", "done_at": None},
         {"id": 9012, "kind": "добавление новых", "order": 4, "subject": "коллекция",
          "address": "ПРОВЕРКА Монарх / Adventure", "status": "к обработке",
          "description": "вторая проверочная", "result": "", "done_at": None}]},
    {"id": 902, "file": "ПРОВЕРКА Most Floor.xlsx", "supplier": "ПРОВЕРКА Most Floor",
     "status": "частично обработан", "ready": False, "has_newer": True, "newer_id": 901,
     "locked_by": "Петров", "locked_until": "2026-09-14T12:10:00",
     "created_at": "2026-09-14T10:00:00", "tasks": []}]}


class Probe:

    def __init__(self, client):
        self.client = client
        self.ok = 0
        self.failed = 0

    def call(self, path, payload=None):
        body = (None if payload is None
                else json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        for attempt in range(5):
            try:
                r = (self.client.get(path) if body is None
                     else self.client.post(path, content=body, headers=JSON_UTF8))
                break
            except NET:
                if attempt == 4:
                    raise
                time.sleep(2 * (attempt + 1))

        text = r.content.decode("utf-8-sig", errors="replace")
        # 1С отдаёт HTML-страницу веб-сервера, когда исключение ушло наружу необработанным
        if "<!DOCTYPE" in text or "<html" in text.lower():
            return r.status_code, {"_html": text[:300]}
        try:
            return r.status_code, json.loads(text)
        except ValueError:
            return r.status_code, {"_raw": text[:300]}

    def check(self, title, cond, detail=""):
        if cond:
            self.ok += 1
            print("  [ok]   %s" % title)
        else:
            self.failed += 1
            print("  [FAIL] %s  %s" % (title, detail))

    def run(self):
        print("1. GET agent-commands — очередь и часы сервера")
        code, data = self.call("/get-products/agent-commands")
        self.check("HTTP 200", code == 200, "код %s / %s" % (code, str(data)[:200]))
        self.check("есть server_time", bool(data.get("server_time")), str(data)[:200])
        self.check("commands это список", isinstance(data.get("commands"), list),
                   str(data)[:200])
        # Функция ловит своё исключение и кладёт его в `error`, поэтому пустой список
        # команд сам по себе ничего не доказывает: без этой проверки шаг проходил, молча
        # пряча «нет такого регистра».
        self.check("нет поля error", "error" not in data, str(data)[:400])
        self.check("нет поля missing (все объекты заведены)", "missing" not in data,
                   str(data.get("missing")))

        print("\n2. POST set-model-state без ключа prices — зеркало трогать НЕЛЬЗЯ")
        # Ключ обязан быть годным именем свойства 1С: с дефисом или кириллицей тело не
        # прочиталось бы в Структуру вовсе, и проверялась бы не наша ветка.
        code, guard = self.call("/get-products/set-model-state", {"note": "no prices"})
        self.check("отказ prices_missing", guard.get("error") == "prices_missing",
                   str(guard)[:300])
        base = guard.get("version")

        print("\n3. POST set-model-state — первый снимок")
        code, r1 = self.call("/get-products/set-model-state", SNAP)
        self.check("нет ошибки", "error" not in r1, str(r1)[:400])
        if r1.get("detail"):
            print("       ПОДРОБНО: %s" % r1["detail"][:900])
        self.check("прайсов 2", r1.get("prices") == 2, str(r1)[:200])
        self.check("задач 2", r1.get("tasks") == 2, str(r1)[:200])
        self.check("что-то изменилось", (r1.get("changed") or 0) > 0, str(r1)[:200])
        self.check("версия выросла", r1.get("version") != base,
                   "было %s стало %s" % (base, r1.get("version")))

        print("\n4. ПОВТОР ТОГО ЖЕ снимка — разница обязана быть пустой")
        code, r2 = self.call("/get-products/set-model-state", SNAP)
        self.check("changed = 0", r2.get("changed") == 0, str(r2)[:200])
        self.check("версия НЕ выросла", r2.get("version") == r1.get("version"),
                   "было %s стало %s" % (r1.get("version"), r2.get("version")))

        print("\n5. Меняем ОДНО поле — должна измениться одна строка")
        snap3 = json.loads(json.dumps(SNAP))
        task = snap3["prices"][0]["tasks"][0]
        task["status"] = "выполнена"
        task["result"] = "цены записаны"
        task["done_at"] = "2026-09-14T13:05:00"
        code, r3 = self.call("/get-products/set-model-state", snap3)
        self.check("changed = 1", r3.get("changed") == 1, str(r3)[:200])
        self.check("версия выросла на 1", r3.get("version") == (r2.get("version") or 0) + 1,
                   "было %s стало %s" % (r2.get("version"), r3.get("version")))

        print("\n6. Убираем прайс 902 — пропавшее должно удалиться")
        snap4 = json.loads(json.dumps(snap3))
        snap4["prices"] = [p for p in snap4["prices"] if p["id"] != 902]
        code, r4 = self.call("/get-products/set-model-state", snap4)
        self.check("прайсов 1", r4.get("prices") == 1, str(r4)[:200])
        self.check("changed = 1", r4.get("changed") == 1, str(r4)[:200])

        print("\n7. Убираем одну задачу — удаление задач тоже работает")
        snap5 = json.loads(json.dumps(snap4))
        snap5["prices"][0]["tasks"] = snap5["prices"][0]["tasks"][:1]
        code, r5 = self.call("/get-products/set-model-state", snap5)
        self.check("задач 1", r5.get("tasks") == 1, str(r5)[:200])
        self.check("changed = 1", r5.get("changed") == 1, str(r5)[:200])

        print("\n8. Ответ по несуществующей команде — внятный отказ, не падение")
        code, r6 = self.call("/get-products/agent-commands-state",
                             {"commands": [{"id": "нет-такой-команды",
                                            "state": "принята"}]})
        got = (r6.get("results") or [{}])[0]
        self.check("code = command_not_found", got.get("code") == "command_not_found",
                   str(r6)[:300])

        print("\n9. Неизвестное состояние — отвергается со списком допустимых")
        code, r7 = self.call("/get-products/agent-commands-state",
                             {"commands": [{"id": "что-угодно", "state": "потерялась"}]})
        got = (r7.get("results") or [{}])[0]
        self.check("code = state_unknown", got.get("code") == "state_unknown",
                   str(r7)[:300])

        print("\n10. Уборка зеркала — пустой снимок вычищает тестовые строки")
        code, r8 = self.call("/get-products/set-model-state", {"prices": []})
        self.check("прайсов 0", r8.get("prices") == 0, str(r8)[:200])
        self.check("задач 0", r8.get("tasks") == 0, str(r8)[:200])
        code, r9 = self.call("/get-products/set-model-state", {"prices": []})
        self.check("повторная уборка ничего не меняет", r9.get("changed") == 0,
                   str(r9)[:200])


def main() -> int:
    cfg = load_config()
    if not cfg.onec_base_url or not cfg.onec_token:
        print("Нужны ONEC_BASE_URL и ONEC_TOKEN в .env")
        return 2

    client = httpx.Client(base_url=cfg.onec_base_url, timeout=60.0,
                          headers={"X-API-Token": cfg.onec_token})
    try:
        probe = Probe(client)
        probe.run()
    finally:
        client.close()

    print("\n" + "=" * 72)
    print("ПРОШЛО: %d   ПРОВАЛЕНО: %d" % (probe.ok, probe.failed))
    return 1 if probe.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
