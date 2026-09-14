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

import json
import logging

from src.model.enums import TaskKind, TaskSubject
from src.model.refs import Ref, TaskAddress
from src.model.task import PriceTask
from src.price_tool.parser import parse_price_table, render_preview

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
            "tm — марка как она называется в 1С (или как в прайсе, если в 1С её нет); "
            "tm_code — её код, если знаешь. collection — имя коллекции из прайса.\n"
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
        if not item and not collection:
            return "Нужна коллекция либо товар: задача без предмета не адресуема."

        if item:
            subject = Ref.make(article=inp.get("article"), names=[item])
            kind_of = TaskSubject.ITEM
        else:
            subject = Ref.make(names=[collection])
            kind_of = TaskSubject.COLLECTION

        task = PriceTask(
            kind=kind,
            address=TaskAddress(tm=Ref.make(code=inp.get("tm_code"), names=[tm]),
                                subject=subject, subject_kind=kind_of),
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

— «нормализация наименований» — в прайсе видно, как названы коллекции и расцветки;
— «изменение свойств» — в прайсе есть класс, фаска, размер, основа, упаковка;
— «перенос в снятые» — есть повод думать, что часть позиций 1С в прайс не попала;
— «добавление новых» — в прайсе видны позиции, которых может не быть в 1С;
— «изменение цен» — в прайсе есть колонки цен.

Бренда нет среди марок 1С — задач по нему НЕ заводи, но скажи об этом в описании
ближайшей задачи: админ решит, заводить ли марку.

Не дроби без нужды: одна задача на коллекцию и вид. Если коллекций в разделе не видно,
заводи задачу на раздел целиком и так и напиши.

Закончив, коротко ответь, сколько задач завёл и по каким маркам."""


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

    return tools.collected, answer
