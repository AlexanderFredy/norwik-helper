"""Формирование списка задач по прайсу силами LLM (§6.1 specs/agent-workflow-model.md).

Заменяет заглушку из `intake.stub_tasks`. Агент читает прайс, сопоставляет бренды с
торговыми марками 1С и заводит задачи на РЕАЛЬНЫЕ пары (марка, коллекция) — вместо пяти
одинаковых строк на каждый лист.

**СВЕРКА С 1С ЕСТЬ, и задачи из-за неё конкретные.** `compare_with_1c` отвечает, каких
артикулов прайса нет в 1С, каких позиций 1С не стало в прайсе и какие свойства у коллекции
пусты. Без неё агент мог написать только «проверить, все ли позиции заведены» — описание,
после которого работа может оказаться пустой, и админ перестаёт такие читать.

**Выгрузка номенклатуры в контекст НЕ ПОПАДАЕТ.** Код забирает её себе, сравнивает и
возвращает несколько строк. Иначе тысячи токенов поехали бы в КАЖДЫЙ следующий запрос:
цикл ручной, история целиком (§9.6.3 content-manager.md).

Чего сверка не даёт даже так: самих наименований 1С и цен. Поэтому агент по-прежнему не
судит о соответствии имён шаблону — это делает код при выполнении.

**Инструменты возвращают текст, а состояние меняет код.** Задачи копятся в `collected` и
попадают в модель одним куском после хода: так неудачный ход не оставляет половину списка.
"""
from __future__ import annotations

import asyncio
import json
import logging

from src.model.enums import TaskKind, TaskSubject
from src.model.normalize import collection_of
from src.model.refs import Ref, TaskAddress, norm_article
from src.model.task import PriceTask
from src.price_tool.items import build_name
from src.price_tool.parser import parse_price_table, render_preview
from src.price_tool.scope import normalize

logger = logging.getLogger(__name__)

MAX_SHEET_ROWS = 200        # строк листа в один ответ инструмента
MAX_TASKS = 200             # потолок на прогон: защита от разгона, а не рабочий предел


def normalize_brief() -> str:
    """Что написано в описании задачи нормализации. Текст задаёт КОД, а не модель.

    **ЗАЧЕМ ОТБИРАТЬ У НЕЁ ЭТО ОПИСАНИЕ.** Агент дважды выдумывал шаблон и оба раза
    неверно — последний раз «марка + коллекция + декор + артикул», хотя артикул в
    наименованиях 1С не используется ВООБЩЕ (§19.5), а вид товара стоит первым. Выгрузки
    номенклатуры у него в этот момент нет, сверять не с чем, и запрет в промпте помогает
    ненадёжно: он описывает то, что видит в прайсе, и принимает это за стандарт 1С.

    Записать в 1С выдумку он не может — `run_normalization` описание не читает вовсе, имена
    собирает `build_name`. Но описание читает АДМИН, и неверный стандарт в нём — это
    дезинформация человека, который по нему принимает решения.

    **Пример собирается `build_name`, а не переписан руками.** Строка из неё не разойдётся
    с тем, что код правда делает: поменяется шаблон — поменяется и описание.
    """
    sample = build_name("Ламинат", "Egger", "Vintage", "Дуб Медовый")
    return ("Привести наименования марки к шаблону §19.5: "
            f"[вид товара] [марка] [коллекция] [название] — «{sample}». "
            "Артикул в наименованиях НЕ используется, он отдельный реквизит. "
            "Размер дописывается только у керамики. "
            "Сверку с шаблоном и запись выполняет код.")


TOOLS = [
    {
        "name": "read_price",
        "description": (
            "Показать лист прайса. Параметры: sheet (имя листа; без него — первый), "
            "from_row (с какой непустой строки, с 1). За раз до 200 строк — читай "
            "дальше тем же инструментом, если раздел не поместился."),
        "input_schema": {
            "type": "object",
            "properties": {"sheet": {"type": "string"},
                           "from_row": {"type": "integer"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_selling_tm",
        "description": (
            "Торговые марки 1С: [{name, code, selling}]. `selling: false` — марка заведена, "
            "но не помечена к выгрузке на сайт. Бренда нет в списке совсем — заводить "
            "задачи по нему не нужно, скажи об этом в описании ближайшей задачи."),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "compare_with_1c",
        "description": (
            "Сверить коллекцию из прайса с 1С. Передай tm_code, collection и articles — "
            "СПИСОК артикулов, которые ты увидел в этой коллекции прайса.\n"
            "Вернётся КОРОТКАЯ сводка: чего из прайса нет в 1С, чего из 1С нет в прайсе, "
            "какие свойства у позиций коллекции пустые.\n"
            "ЗОВИ ЭТО ПЕРЕД add_task по каждой коллекции. Без сверки ты можешь написать "
            "только «проверить», а со сверкой — «завести 3 недостающих: …». Выгрузка "
            "номенклатуры при этом в ответ НЕ попадает: сравнение делает код."),
        "input_schema": {
            "type": "object",
            "properties": {
                "tm_code": {"type": "string"},
                "collection": {"type": "string"},
                "articles": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["tm_code", "collection"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_task",
        "description": (
            "Завести задачу по прайсу. ОДНА задача = один вид работы по одной коллекции "
            "(или товару).\n"
            "kind: нормализация наименований | изменение свойств | перенос в снятые | "
            "добавление новых | изменение цен.\n"
            "«НОРМАЛИЗАЦИЯ НАИМЕНОВАНИЙ» — ОДНА НА МАРКУ, коллекцию для неё не указывай: "
            "имена собираются по единому шаблону для всей марки, и дробить работу не по "
            "чему. Передашь коллекцию — она всё равно не учтётся.\n"
            "tm — марка как она называется в 1С (или как в прайсе, если в 1С её нет); "
            "tm_code — её код, если знаешь. collection — имя коллекции из прайса.\n"
            "МАРКУ ПРОВЕРЯЕТ КОД по дереву папок 1С и при расхождении поправит: "
            "принадлежность коллекции марке по вёрстке прайса видна не всегда.\n"
            "description — что КОНКРЕТНО предстоит сделать, с числами и артикулами из "
            "`compare_with_1c`. «Проверить, все ли заведены» — плохое описание: это ты "
            "уже проверил. «Завести 3 позиции: 3311, 3315, 3316» — хорошее.\n"
            "Повторный вызов с тем же адресом и видом дополняет описание, а не плодит "
            "вторую задачу."),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "tm": {"type": "string"},
                "tm_code": {"type": "string"},
                "collection": {"type": "string"},
                "item": {"type": "string", "description": "если задача про один товар"},
                "article": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["kind", "tm", "description"],
            "additionalProperties": False,
        },
    },
]


class TaskBuilderTools:
    """Исполнитель инструментов одного прогона формирования задач."""

    def __init__(self, content: bytes, filename: str, onec=None) -> None:
        self._content = content
        self._filename = filename
        self._onec = onec
        self.collected: list[PriceTask] = []
        self.marks: list[dict] = []
        self._sheets = None
        self._items_cache: dict[str, list] = {}

    def handles(self, name: str) -> bool:
        return name in {t["name"] for t in TOOLS}

    @property
    def sheets(self):
        if self._sheets is None:
            try:
                self._sheets = list(parse_price_table(self._content, self._filename) or [])
            except Exception:                           # noqa: BLE001
                logger.warning("Не разобрался прайс %s", self._filename, exc_info=True)
                self._sheets = []
        return self._sheets

    async def execute(self, name: str, inp: dict):
        try:
            if name == "read_price":
                return self._read(inp)
            if name == "get_selling_tm":
                return self._tm()
            if name == "compare_with_1c":
                return self._compare(inp)
            if name == "add_task":
                return self._add(inp)
        except Exception as exc:                        # noqa: BLE001
            logger.exception("Инструмент %s сорвался", name)
            return f"Ошибка инструмента: {exc}"
        return f"Неизвестный инструмент: {name}"

    # ------------------------------------------------------------ инструменты

    def _read(self, inp: dict) -> str:
        if not self.sheets:
            return ("Файл не разобрался. Заводи задачи по тому, что известно из имени "
                    "файла, и скажи об этом в описании.")
        wanted = (inp.get("sheet") or "").strip().lower()
        sheet = next((s for s in self.sheets if s.name.lower() == wanted), None) \
            or self.sheets[0]
        start = max(1, int(inp.get("from_row") or 1))

        head = (f"Листы: {', '.join(s.name for s in self.sheets)}\n"
                f"=== Лист: {sheet.name} === (со строки {start})\n")
        return head + render_preview(sheet, max_rows=MAX_SHEET_ROWS, start=start)

    def _tm(self) -> str:
        if self._onec is None:
            return "[]"
        marks = self._onec.selling_tm(all_marks=True)
        self.marks = [{"name": m.name, "code": m.code, "selling": m.selling}
                      for m in marks]
        return json.dumps(self.marks, ensure_ascii=False)

    def _compare(self, inp: dict) -> str:
        """Сверка коллекции прайса с 1С. Наружу идёт СВОДКА, не выгрузка.

        **В ЭТОМ ВСЯ ЭКОНОМИЯ.** Номенклатура марки — тысячи токенов, и, попав в ответ
        инструмента, она поедет в КАЖДЫЙ следующий запрос к модели (цикл ручной, история
        целиком). Здесь код забирает её себе, сравнивает и возвращает несколько строк.
        Агент получает то, ради чего сверка нужна, не платя за неё контекстом.

        Сопоставление по артикулу через `norm_article`: «LE-263», «LE 263» и «le263» —
        один артикул, поставщики пишут как придётся.
        """
        if self._onec is None:
            return ("1С не настроена — сверить не с чем. Заводи задачи по прайсу и пиши "
                    "«проверить», а не «завести».")

        tm_code = str(inp.get("tm_code") or "").strip()
        collection = str(inp.get("collection") or "").strip()
        if not tm_code or not collection:
            return "Нужны tm_code и collection."

        items = self._nomenclature(tm_code)
        wanted = normalize(collection)
        # Коллекция берётся `collection_of`: у части марок свойство пустое, и остаётся имя
        # папки — с размером внутри, который к имени коллекции не относится.
        mine = [i for i in items
                if normalize(collection_of(i)) == wanted and not i.not_exported]

        from_price = []
        seen = set()
        for raw in (inp.get("articles") or []):
            key = norm_article(raw)
            if key and key not in seen:
                seen.add(key)
                from_price.append((key, str(raw).strip()))

        in_1c = {norm_article(i.article): i for i in mine if i.article}

        missing_in_1c = [shown for key, shown in from_price if key not in in_1c]
        # Обратная сторона: позиция есть в 1С, но в прайсе её не стало — кандидат в
        # снятые. Только КАНДИДАТ: поставщик мог прислать частичный прайс, и решать это
        # админу, а не прогону.
        missing_in_price = [f"{i.article} {i.site_name or i.name}".strip()
                            for key, i in in_1c.items() if key not in seen]

        empty = self._empty_properties(mine)

        out = {
            "collection": collection,
            "in_1c": len(mine),
            "from_price": len(from_price),
            "missing_in_1c": missing_in_1c[:40],
            "missing_in_price": missing_in_price[:40],
            "empty_properties": empty,
        }
        if not mine:
            out["note"] = ("коллекции в 1С нет вовсе — её придётся заводить вместе с "
                           "позициями")
        return json.dumps(out, ensure_ascii=False)

    @staticmethod
    def _empty_properties(items) -> list[str]:
        """Свойства, не заполненные НИ У ОДНОЙ позиции коллекции.

        Именно «ни у одной», а не «у какой-нибудь»: разнобой внутри коллекции — отдельный
        разговор, а вот свойство, пустое сплошь, скорее всего просто не заводили.
        """
        if not items:
            return []
        filled: set[str] = set()
        known: set[str] = set()
        for item in items:
            for prop in item.properties:
                known.add(prop.property)
                if prop.value:
                    filled.add(prop.property)
        return sorted(known - filled)

    def _nomenclature(self, tm_code: str) -> list:
        """Выгрузка марки с кешем на прогон: за составление задач она не меняется."""
        if tm_code not in self._items_cache:
            nom = self._onec.by_tm_all(tm_code, include_not_exported=True)
            self._items_cache[tm_code] = list(nom.items)
        return self._items_cache[tm_code]

    def _add(self, inp: dict) -> str:
        if len(self.collected) >= MAX_TASKS:
            return f"Достигнут потолок в {MAX_TASKS} задач — заканчивай."

        raw = (inp.get("kind") or "").strip().lower()
        kind = next((k for k in TaskKind if k.value == raw), None)
        if kind is None:
            return ("Неизвестный вид задачи. Допустимо: "
                    + ", ".join(k.value for k in TaskKind))

        tm = (inp.get("tm") or "").strip()
        if not tm:
            return "Не указана марка — без неё задача не адресуема."

        item = (inp.get("item") or "").strip()
        collection = (inp.get("collection") or "").strip()
        mark = Ref.make(code=inp.get("tm_code"), names=[tm])

        # Нормализация адресуется МАРКОЙ, поэтому предмет ей не нужен — требовать его
        # значило бы отклонять правильно составленную задачу.
        if kind != TaskKind.NORMALIZE_NAMES and not item and not collection:
            return "Нужна коллекция либо товар: задача без предмета не адресуема."

        if kind == TaskKind.NORMALIZE_NAMES:
            # НОРМАЛИЗАЦИЯ — ВСЕГДА НА МАРКУ ЦЕЛИКОМ, что бы ни передала модель.
            #
            # Имена собираются по ЕДИНОМУ шаблону §19.5, одинаковому для всех коллекций
            # марки, и выполняет её код за один проход по выгрузке (`normalize.py`).
            # Разбиение по коллекциям не даёт ничего, а стоит пяти захватов, пяти прогонов
            # и пяти строк в списке вместо одной.
            #
            # Сделано здесь, а не подсказкой в промпте: модель перечисляет коллекции,
            # потому что видит их в прайсе, и помнить исключение для одного вида задач
            # ей неоткуда.
            subject, kind_of = mark, TaskSubject.MARK
            # Стандарт формулирует КОД (см. `normalize_brief`). Текст агента остаётся
            # НИЖЕ и подписан как наблюдение по прайсу: в прайсе он правда кое-что видит
            # — два написания бренда, пометки NEW/АКЦИЯ в заголовках, — и терять это не
            # надо. Но стандартом 1С это не является, и выдавать одно за другое нельзя.
            seen = (inp.get("description") or "").strip()
            description = normalize_brief()
            if seen:
                description += f"\n\nЗамечено в прайсе (наблюдение агента): {seen}"
        elif item:
            subject = Ref.make(article=inp.get("article"), names=[item])
            kind_of = TaskSubject.ITEM
            description = (inp.get("description") or "").strip()
        else:
            subject = Ref.make(names=[collection])
            kind_of = TaskSubject.COLLECTION
            description = (inp.get("description") or "").strip()

        task = PriceTask(
            kind=kind,
            address=TaskAddress(tm=mark, subject=subject, subject_kind=kind_of),
            description=description)

        # Уникальность пары (адрес, вид) держим здесь же: модель может прийти к той же
        # задаче дважды, и это дополнение описания, а не вторая задача (§3.3).
        twin = next((t for t in self.collected if t.matches(task)), None)
        if twin is not None:
            twin.absorb(task)
            return f"Дополнил задачу «{twin.label()}»."

        self.collected.append(task)
        return f"Задача заведена: {task.label()}. Всего: {len(self.collected)}."


PROMPT = """Ты — контент-менеджер интернет-магазина напольных покрытий. Тебе дали прайс
поставщика. Твоя работа сейчас — СОСТАВИТЬ СПИСОК ЗАДАЧ по нему, а не выполнять их.

Порядок:

1. `read_price` — посмотри, что в файле: какие листы, какие бренды и коллекции. Длинный
   лист дочитывай через `from_row`, пока не увидишь все разделы.
2. `get_selling_tm` — сопоставь бренды прайса с марками 1С. Названия бывают двуязычными
   («Latin / Кириллица») — сравнивай без учёта регистра и пунктуации.
3. `compare_with_1c` НА КАЖДУЮ КОЛЛЕКЦИЮ — передай артикулы, которые увидел в ней.
   Вернётся, чего нет в 1С, чего нет в прайсе и какие свойства пустые.
4. `add_task` на каждую пару (марка, коллекция), по которой есть что делать.

ПИШИ ТО, ЧТО ЗНАЕШЬ, А НЕ ТО, ЧТО НАДО ПРОВЕРИТЬ. После `compare_with_1c` ты знаешь
точно — значит и задача должна быть точной:

  плохо: «Проверить, все ли 8 декоров заведены в 1С»
  хорошо: «Завести 3 позиции, которых нет в 1С: 3311 Бетховен, 3315 Рахманинов,
           3316 Глинка»

  плохо: «Проверить и заполнить свойства позиций коллекции»
  хорошо: «У всех 12 позиций пусты «Класс износостойкости» и «Фаска». В прайсе:
           класс 34, 4-сторонняя V-образная фаска»

Сверка показала, что расхождений нет — ЗАДАЧУ НЕ ЗАВОДИ ВОВСЕ. Список задач, где половина
закроется словами «всё уже сделано», обесценивает вторую половину: админ перестаёт их
читать.

ЧЕГО СВЕРКА НЕ РЕШАЕТ ЗА ТЕБЯ. «Нет в прайсе» — это КАНДИДАТ в снятые, а не приговор:
поставщик мог прислать частичный прайс. Так и пиши: «в прайсе не встретились N позиций,
проверить, сняты ли они с производства».

ЧЕГО У ТЕБЯ НЕТ ДАЖЕ СО СВЕРКОЙ: самих наименований 1С и цен. `compare_with_1c` отвечает
на три вопроса — чего нет в 1С, чего нет в прайсе, какие свойства пусты, — и только на них.

— НЕ рассуждай о том, соответствуют ли наименования 1С шаблону: ты их не видел. Шаблон
  задан §19.5 и живёт в коде — он единственный судья, и сверку делает он.

— НЕ утверждай того, чего не проверял. Спросил `compare_with_1c` — пиши числа и артикулы.
  Не спросил — не пиши ни «завести 12 позиций», ни «проверить»: сначала спроси.
— В описании пиши, ЧТО предстоит выяснить и ПОЧЕМУ ты завёл эту задачу: на что в прайсе
  опёрся, какие строки или колонки увидел.

Виды задач и когда их заводить:

— «нормализация наименований» — ОДНА НА МАРКУ, без коллекции. **Не разбирай, что именно
  не так с именами, и не описывай шаблон.** Наименования собирает КОД по §19.5, он же
  сравнивает их с текущими и сам доложит расхождения. Самих наименований 1С ты не видишь
  — `compare_with_1c` их не возвращает, — значит судить о них тебе не по чему.
  Описание пиши коротко: «привести наименования марки к шаблону §19.5». Если в ПРАЙСЕ
  видно что-то, чего код не узнает сам (пометки NEW/АКЦИЯ в заголовках, два написания
  бренда), — добавь одной строкой, но как наблюдение по прайсу, а не как диагноз по 1С;
— «изменение свойств» — сверка показала ПУСТЫЕ свойства, а в прайсе есть чем их
  заполнить: класс, фаска, размер, основа, упаковка. Назови и свойства, и значения;
— «перенос в снятые» — сверка нашла позиции 1С, которых в прайсе не встретилось. Назови
  сколько и какие, и напиши, что это кандидаты: прайс мог быть частичным;
— «добавление новых» — сверка нашла артикулы прайса, которых нет в 1С. Перечисли их;
— «изменение цен» — в прайсе есть колонки цен.

ОДИН ПРАЙС — НЕ ОДНА МАРКА. Поставщик присылает файл со своим именем на обложке, но
коллекции внутри могут принадлежать РАЗНЫМ маркам 1С: у Most Flooring часть коллекций
заведена под A+ Floor. По заголовкам это видно не всегда, поэтому не приписывай всё
подряд марке из имени файла — сомневаешься, так и скажи в описании. Принадлежность
проверит код по дереву папок 1С и поправит, если ты ошибся.

Бренда нет среди марок 1С — задач по нему НЕ заводи, но скажи об этом в описании
ближайшей задачи: админ решит, заводить ли марку.

Не дроби без нужды: одна задача на коллекцию и вид. Если коллекций в разделе не видно,
заводи задачу на раздел целиком и так и напиши.

Закончив, коротко ответь, сколько задач завёл и по каким маркам."""


def owner_map(folders, marks) -> dict[str, set]:
    """Нормализованное имя коллекции → марки 1С, которым она принадлежит.

    Строится по дереву папок: от папки коллекции вверх по `parent_ref` до папки с
    `kind == "tm"`, её имя сопоставляется со списком марок ради КОДА — в адресе задачи
    нужен именно он.

    Значение — МНОЖЕСТВО: одноимённые коллекции у разных марок бывают («Дуб», «Классик»),
    и тогда однозначного владельца нет. Такое лучше оставить как есть, чем переставить
    наугад.
    """
    by_ref = {f.ref: f for f in folders}
    by_name = {normalize(m["name"]): m for m in marks if m.get("name")}

    owners: dict[str, set] = {}
    for folder in folders:
        if folder.kind != "collection":
            continue

        node, guard = by_ref.get(folder.parent_ref), 0
        while node is not None and node.kind != "tm" and guard < 20:
            node = by_ref.get(node.parent_ref)
            guard += 1                      # дерево битое — цикл не вечный

        if node is None or node.kind != "tm":
            continue

        mark = by_name.get(normalize(node.name))
        if mark is None:
            continue

        owners.setdefault(normalize(folder.name), set()).add(
            (mark["code"], mark["name"]))

    return owners


def fix_marks(tasks: list[PriceTask], owners: dict[str, set]) -> list[str]:
    """Поправить марку в адресах задач по 1С. Возвращает заметки о правках.

    **ЗАЧЕМ.** Марку для коллекции агент выводит из вёрстки прайса, а она обманчива: у
    Most Flooring часть коллекций принадлежит марке A+ Floor, и по заголовкам файла это
    не видно. Ошибка дорогая — задача уезжает не на ту марку, и прогон правит чужой
    справочник либо не находит ничего.

    1С знает владельца точно. Значит спрашивать модель не о чем: правим молча, а говорим
    только о том, чего не разрешили.
    """
    notes = []
    for task in tasks:
        if task.subject != TaskSubject.COLLECTION:
            continue

        # Ключ карты — НОРМАЛИЗОВАННОЕ имя, а в `names` теперь лежит исходное написание:
        # «Ле Паркет» не совпало бы с ключом «ле паркет» без приведения.
        found = None
        for key in task.address.subject.keys:
            found = owners.get(key)
            if found:
                break
        if not found:
            continue

        if len(found) > 1:
            notes.append(f"«{task.address.subject.label()}» есть у нескольких марок — "
                         f"оставил {task.address.tm.label()}")
            continue

        code, mark_name = next(iter(found))
        if task.address.tm.code == code:
            continue

        was = task.address.tm.label()
        task.address = TaskAddress(tm=Ref.make(code=code, names=[mark_name]),
                                   subject=task.address.subject,
                                   subject_kind=task.subject)
        notes.append(f"«{task.address.subject.label()}»: марка {was} → {mark_name}")

    return notes


async def build(orchestrator, content: bytes, filename: str, onec=None,
                usage_labels: dict | None = None) -> tuple[list[PriceTask], str]:
    """Прогон формирования задач. Возвращает (задачи, короткий ответ агента).

    Пустой список — не ошибка: агент мог не найти, за что зацепиться. Вызывающий решает,
    падать ли обратно на заглушку.
    """
    tools = TaskBuilderTools(content, filename, onec=onec)
    task = f"Прайс «{filename}». Составь список задач по нему."

    answer, _ = await orchestrator.handle_turn(
        [{"role": "user", "content": task}], system=PROMPT, extra_tools=TOOLS,
        extra_executor=tools, base_tools=False, usage_labels=usage_labels)

    # СВЕРКА МАРОК ПО 1С — ПОСЛЕ хода и БЕЗ участия модели.
    #
    # Это обычный запрос к 1С из кода, а не вызов инструмента: круг ручного цикла несёт
    # всю историю и стоит ~$0.097, а здесь не нужно ни решения, ни рассуждения — только
    # дерево папок. Поэтому сверка бесплатна.
    if onec is not None and tools.collected and tools.marks:
        try:
            tree = await asyncio.to_thread(onec.folders)
            notes = fix_marks(tools.collected, owner_map(tree.items, tools.marks))
            for note in notes:
                logger.info("Марка в задаче поправлена по 1С: %s", note)
            if notes:
                answer += "\n\nМарки сверены с 1С: " + "; ".join(notes) + "."
        except Exception:                               # noqa: BLE001
            # Сверка — улучшение, а не условие работы: без неё задачи остаются такими,
            # какими их составил агент, и это прежнее поведение, а не поломка.
            logger.warning("Не удалось сверить марки задач с 1С", exc_info=True)

    return tools.collected, answer
