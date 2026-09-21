"""Формирование списка задач по прайсу силами LLM (§6.1 specs/agent-workflow-model.md).

Заменяет заглушку из `intake.stub_tasks`. Агент читает прайс, сопоставляет бренды с
торговыми марками 1С и заводит задачи на РЕАЛЬНЫЕ пары (марка, коллекция) — вместо пяти
одинаковых строк на каждый лист.

**Что агент делает и чего НЕ делает.** Он читает файл и справочник марок 1С, после чего
называет разделы и коллекции. Сверки с номенклатурой 1С здесь пока нет — значит задачи вида
«перенос в снятые» и «добавление новых» заводятся как ПОВОД ПРОВЕРИТЬ, а не как готовый
диффом список позиций. Это следующий шаг; описание задачи должно честно говорить, что
именно предстоит выяснить.

**Инструменты возвращают текст, а состояние меняет код.** Задачи копятся в `collected` и
попадают в модель одним куском после хода: так неудачный ход не оставляет половину списка.
"""
from __future__ import annotations

import asyncio
import json
import logging

from src.model.enums import TaskKind, TaskSubject
from src.model.refs import Ref, TaskAddress
from src.model.task import PriceTask
from src.price_tool.parser import parse_price_table, render_preview
from src.price_tool.scope import normalize

logger = logging.getLogger(__name__)

MAX_SHEET_ROWS = 200        # строк листа в один ответ инструмента
MAX_TASKS = 200             # потолок на прогон: защита от разгона, а не рабочий предел


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
            "description — что КОНКРЕТНО предстоит сделать и на основании чего ты так "
            "решил. Сверки с номенклатурой 1С у тебя сейчас НЕТ, поэтому не утверждай, "
            "чего не проверял: пиши «проверить», а не «удалить 12 позиций».\n"
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
        elif item:
            subject = Ref.make(article=inp.get("article"), names=[item])
            kind_of = TaskSubject.ITEM
        else:
            subject = Ref.make(names=[collection])
            kind_of = TaskSubject.COLLECTION

        task = PriceTask(
            kind=kind,
            address=TaskAddress(tm=mark, subject=subject, subject_kind=kind_of),
            description=(inp.get("description") or "").strip())

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
3. `add_task` на каждую пару (марка, коллекция), по которой есть что делать.

ЧЕГО У ТЕБЯ СЕЙЧАС НЕТ: выгрузки номенклатуры 1С. Ты не знаешь, какие позиции там уже
заведены, какие цены стоят и чего не хватает. Поэтому:

— НЕ утверждай того, чего не проверял. «Проверить, все ли позиции коллекции заведены» —
  честно. «Завести 12 недостающих позиций» — выдумка, у тебя нет этих данных.
— В описании пиши, ЧТО предстоит выяснить и ПОЧЕМУ ты завёл эту задачу: на что в прайсе
  опёрся, какие строки или колонки увидел.

Виды задач и когда их заводить:

— «нормализация наименований» — ОДНА НА МАРКУ, без коллекции: имена собираются по
  единому шаблону, и дробить работу не по чему;
— «изменение свойств» — в прайсе есть класс, фаска, размер, основа, упаковка;
— «перенос в снятые» — есть повод думать, что часть позиций 1С в прайс не попала;
— «добавление новых» — в прайсе видны позиции, которых может не быть в 1С;
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
