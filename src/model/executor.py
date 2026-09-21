"""Выполнение ОДНОЙ задачи по прайсу силами LLM (§6.2 specs/agent-workflow-model.md).

Заменяет заглушку в `PriceListService._execute`: здесь агент действительно читает 1С и
пишет в неё. Задача приходит с адресом (марка + коллекция или товар), видом работы и
описанием, которое мог поправить админ прямо перед запуском.

**ЗАПИСЬ ЗАЩИЩЕНА МАРКЕРОМ ПОКОЛЕНИЯ, и проверка стоит НЕПОСРЕДСТВЕННО ПЕРЕД ней.** Между
отменой прогона и записью есть промежуток, и необратима именно запись: захват мог истечь,
админ мог снять его руками, прайс могли уничтожить. Проверять только перед сменой статуса
недостаточно — статус поправим, запись в 1С нет.

**ПРАВИЛА СБОРКИ НЕ ПЕРЕПИСЫВАЮТСЯ ЗАНОВО.** Наименования (§19.5), округление розницы,
порог значимости цены, проверка категорий — всё это уже живёт в `price_tool` и вызывается
отсюда. Второй набор правил разошёлся бы с первым молча: запись прошла бы, а имена
получились бы другими.

**ОТЧЁТ ЧЕСТНЫЙ.** Исход «частично обработана» — штатный: часть позиций записалась, часть
нет, и в результате обязаны стоять причина и что именно сделано. «Не получилось ничего»
оставляет задачу в «к обработке», а не выдаёт за частичный успех.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from decimal import Decimal

from src.model import normalize as nz
from src.model.enums import TaskKind, TaskStatus, TaskSubject
from src.price_tool import items as item_rules
# `plan_collection` есть И в `changes` (цены), И в `items` (справочник) — разные функции
# с одним именем. Ценовую берём под своим именем, справочную зовём через модуль:
# перепутать их значит собрать не тот payload и записать не то.
from src.price_tool.changes import build_payload, plan_items
from src.price_tool.changes import plan_collection as plan_price_group
from src.price_tool.parser import render_preview, parse_price_table

logger = logging.getLogger(__name__)

MAX_SHEET_ROWS = 200
MAX_ITEMS_SHOWN = 400        # позиций 1С в один ответ инструмента


class WriteRefused(Exception):
    """Запись отклонена маркером поколения — прогон больше не имеет на неё права."""


TOOLS = [
    {
        "name": "read_price",
        "description": (
            "Показать лист прайса. Параметры: sheet (имя листа; без него — первый), "
            "from_row (с какой непустой строки, с 1). За раз до 200 строк."),
        "input_schema": {
            "type": "object",
            "properties": {"sheet": {"type": "string"},
                           "from_row": {"type": "integer"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_1c_items",
        "description": (
            "ЖИВАЯ номенклатура 1С по марке: [{ref, article, name, full_name, "
            "collection, collection_ref, product_type, unit, цены, свойства}]. "
            "Параметр tm_code обязателен, collection — необязательный фильтр.\n"
            "СНЯТЫЕ СЮДА НЕ ПОПАДАЮТ. Часть коллекций висит под маркой с флагом «Не "
            "выгружать» — они уже сняты, работы по ним НЕТ, предлагать ничего не нужно. "
            "Найти снятую позицию (проверка на дубль перед созданием) — `find_1c_items`.\n"
            "ВЫЗЫВАЙ ОДИН РАЗ на марку: выгрузка большая и целиком едет в каждый "
            "следующий запрос."),
        "input_schema": {
            "type": "object",
            "properties": {"tm_code": {"type": "string"},
                           "collection": {"type": "string"}},
            "required": ["tm_code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_1c_items",
        "description": (
            "Поиск по ВСЕЙ номенклатуре 1С, включая снятые с производства и чужие марки. "
            "Нужен перед созданием позиций: товар мог быть заведён под другой маркой, и "
            "тогда создание даст дубль. Передавай articles СПИСКОМ — до 100 за раз, "
            "один вызов на коллекцию."),
        "input_schema": {
            "type": "object",
            "properties": {
                "articles": {"type": "array", "items": {"type": "string"}},
                "name": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_1c_folders",
        "description": (
            "Папки марки: дерево коллекций с кодами. Нужно, чтобы узнать parent_ref для "
            "создания позиций и куда переносить снятые. Один вызов на марку."),
        "input_schema": {
            "type": "object",
            "properties": {"tm_code": {"type": "string"}},
            "required": ["tm_code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_1c_properties",
        "description": (
            "Свойства вида товара и их допустимые значения с кодами. Без этого нельзя "
            "заполнить properties у позиции: значение задаётся КОДОМ, не текстом. "
            "Один вызов на вид товара."),
        "input_schema": {
            "type": "object",
            "properties": {"product_type": {"type": "string"}},
            "required": ["product_type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_items",
        "description": (
            "ЗАПИСАТЬ правки справочника по ОДНОЙ коллекции: создать позиции, поправить "
            "поля, перенести в другую папку. Пишет в 1С СРАЗУ — админ уже подтвердил "
            "работу, нажав «Выполнить».\n"
            "НАИМЕНОВАНИЯ СОБИРАЕТ КОД. Передавай ЧАСТИ: `title` — только название "
            "расцветки («Дуб Медовый»), `tail` — размер, которым поставщик различает "
            "позиции. Имя, полное наименование и наименование для сайта соберутся сами.\n"
            "ИМЯ МАРКИ БЕРЁТСЯ ИЗ 1С и переданное тобой не используется: как пишется "
            "марка — решает справочник, а не прайс. Написания разошлись — скажи об этом "
            "в finish, менять его вправе только админ.\n"
            "Передавай только то, что МЕНЯЕТСЯ: отсутствие поля значит «не трогать».\n"
            "СВОЙСТВО «Коллекция» (код 0000003) НЕ ЗАПОЛНЯЙ. Пустое оно остаётся пустым: "
            "в выгрузке коллекция могла быть выведена из имени папки, а имя папки несёт "
            "размер и меняется при пересортировке справочника. Поставить это значение "
            "вправе только админ.\n"
            "Цены сюда НЕ передаются — для них отдельный инструмент."),
        "input_schema": {
            "type": "object",
            "properties": {
                "tm_code": {"type": "string"},
                "tm_name": {"type": "string"},
                "product_type": {"type": "string", "description": "код вида товара"},
                "product_type_name": {"type": "string"},
                "collection": {"type": "string"},
                "new_folder": {
                    "type": "object",
                    "description": "создать папку коллекции: parent_ref — код папки ТМ",
                    "properties": {"parent_ref": {"type": "string"},
                                   "name": {"type": "string"}},
                },
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "enum": ["create", "update"]},
                            "ref": {"type": "string", "description": "код 1С для update"},
                            "article": {"type": "string"},
                            "title": {"type": "string",
                                      "description": "ТОЛЬКО название расцветки"},
                            "tail": {"type": "string", "description": "размер в конце имени"},
                            "parent_ref": {"type": "string",
                                           "description": "куда положить или перенести"},
                            "unit": {"type": "string"},
                            "pack_coefficient": {"type": "number"},
                            "length": {"type": "number"},
                            "width": {"type": "number"},
                            "thickness": {"type": "number"},
                            "properties": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {"property": {"type": "string"},
                                                   "value_code": {"type": "string"}},
                                },
                            },
                        },
                        "required": ["op"],
                    },
                },
            },
            "required": ["tm_code", "product_type", "collection"],
        },
    },
    {
        "name": "write_prices",
        "description": (
            "ЗАПИСАТЬ цены по ОДНОЙ коллекции. Пишет в 1С СРАЗУ. Передавай закупочную "
            "цену ИЗ ПРАЙСА, приведённую к базовой ЕИ товара; розница считается сама по "
            "правилам магазина — её НЕ передавай.\n"
            "ДВА СПОСОБА: purchase на всю коллекцию (ламинат — коллекция это один декор "
            "в разных цветах) либо items[{ref, purchase}] по товарам (КЕРАМИКА: в одной "
            "папке настенная, напольная, декор, бордюр — у каждого своя цена).\n"
            "Изменения меньше 2% отбрасываются сами."),
        "input_schema": {
            "type": "object",
            "properties": {
                "tm_code": {"type": "string"},
                "tm_name": {"type": "string"},
                "collection": {"type": "string"},
                "collection_ref": {"type": "string"},
                "purchase": {"type": "number", "description": "цена на всю коллекцию"},
                "items": {
                    "type": "array",
                    "description": "цены по товарам: [{ref, purchase}]",
                    "items": {
                        "type": "object",
                        "properties": {"ref": {"type": "string"},
                                       "purchase": {"type": "number"}},
                    },
                },
            },
            "required": ["tm_code", "collection"],
        },
    },
    {
        "name": "finish",
        "description": (
            "Закончить задачу и доложить исход. Вызывай ОДИН раз, последним.\n"
            "status: «выполнена» — сделано всё задуманное; «частично обработана» — часть "
            "не удалась, и тогда в result ОБЯЗАТЕЛЬНЫ причина и что именно сделано; "
            "«к обработке» — не получилось ничего, задача возвращается в очередь.\n"
            "result — ЧТО ИЗМЕНЕНО В 1С, числами и кодами. Коротко: несколько строк, а "
            "не отчёт. НЕ пересказывай ход работы, не перечисляй, что проверил и почему "
            "решил, не подводи итогов по прайсу целиком — админ видел задачу и читает "
            "только исход. Соседнее расхождение, если заметил, — ОДНОЙ строкой в конце."),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string",
                           "enum": ["выполнена", "частично обработана", "к обработке"]},
                "result": {"type": "string"},
            },
            "required": ["status", "result"],
            "additionalProperties": False,
        },
    },
]


#: Имена инструментов — из самих определений, а не списком рядом: разъехавшись, они дали
#: бы `handles`, который не признаёт собственный инструмент, и вызов ушёл бы менеджерскому
#: исполнителю, где такого имени нет.
_TOOL_NAMES = frozenset(t["name"] for t in TOOLS)


class TaskTools:
    """Инструменты одной задачи. Состояние — что записано и чем закончилось."""

    def __init__(self, onec, content: bytes, filename: str, guard, scope=None,
                 kind: TaskKind = TaskKind.CHANGE_PRICES) -> None:
        self._onec = onec
        self._content = content
        self._filename = filename
        # Вид задачи решает ОБЪЁМ выгрузки 1С: справочные поля ценовой задаче не нужны.
        self._kind = kind
        # `guard()` — проверка маркера поколения. Зовётся ПЕРЕД КАЖДОЙ ЗАПИСЬЮ, а не один
        # раз на старте: прогон длится минуты, и право на запись за это время может
        # пропасть.
        self._guard = guard
        self._scope = list(scope or [])
        self._sheets = None
        self._items_cache: dict[str, list] = {}
        self._tm_names: dict[str, str] = {}     # код марки → её имя В 1С, не в прайсе

        self.written_items = 0
        self.written_prices = 0
        self.failed = 0
        self.errors: list[str] = []
        self.outcome: tuple[TaskStatus, str] | None = None

    # ------------------------------------------------------------------ прайс

    @property
    def sheets(self):
        if self._sheets is None:
            try:
                self._sheets = list(parse_price_table(self._content, self._filename) or [])
            except Exception:                           # noqa: BLE001
                logger.warning("Не разобрался прайс %s", self._filename, exc_info=True)
                self._sheets = []
        return self._sheets

    # ------------------------------------------------------------ диспетчер

    def handles(self, name: str) -> bool:
        """Мои ли это инструменты. Оркестратор спрашивает ПЕРЕД каждым вызовом.

        Без этого метода `handle_turn` падает с `AttributeError` на первом же инструменте
        — и падает ВЕСЬ прогон, а не отдельный вызов. Поймано на бою 21.09.2026: задача
        «перенос в снятые» вернула «прогон сорвался», притом что все инструменты по
        отдельности работали. Нормализация при этом шла нормально — она модель не зовёт.
        """
        return name in _TOOL_NAMES

    async def execute(self, name: str, inp: dict):
        try:
            if name == "read_price":
                return self._read(inp)
            if name == "get_1c_items":
                return await self._items(inp)
            if name == "find_1c_items":
                return await self._find(inp)
            if name == "get_1c_folders":
                return await self._folders(inp)
            if name == "get_1c_properties":
                return await self._properties(inp)
            if name == "write_items":
                return await self._write_items(inp)
            if name == "write_prices":
                return await self._write_prices(inp)
            if name == "finish":
                return self._finish(inp)
        except WriteRefused as exc:
            # Не прячем за общей формулировкой: это не сбой, а отзыв права на запись, и
            # агент должен прекратить, а не пробовать снова.
            return f"ЗАПИСЬ ОТМЕНЕНА: {exc}. Прекрати работу и вызови finish со статусом " \
                   "«к обработке»."
        except Exception as exc:                        # noqa: BLE001
            logger.exception("Инструмент %s сорвался", name)
            return f"Ошибка инструмента: {exc}"
        return f"Неизвестный инструмент: {name}"

    # ------------------------------------------------------------ инструменты

    def _read(self, inp: dict) -> str:
        if not self.sheets:
            return "Файл прайса не разобрался — работать по нему нечем."
        wanted = (inp.get("sheet") or "").strip().lower()
        sheet = next((s for s in self.sheets if s.name.lower() == wanted), None) \
            or self.sheets[0]
        start = max(1, int(inp.get("from_row") or 1))
        head = (f"Листы: {', '.join(s.name for s in self.sheets)}\n"
                f"=== Лист: {sheet.name} === (со строки {start})\n")
        return head + render_preview(sheet, max_rows=MAX_SHEET_ROWS, start=start)

    async def _nomenclature(self, tm_code: str) -> list:
        """Выгрузка марки с кешем на прогон: справочник за задачу не меняется до записи."""
        if tm_code not in self._items_cache:
            nom = await asyncio.to_thread(self._onec.by_tm_all, tm_code,
                                          include_not_exported=True)
            self._items_cache[tm_code] = list(nom.items)
            # Каноническое имя марки — из 1С, и только оттуда (см. `_canonical_tm`).
            self._tm_names[tm_code] = (nom.tm or "").strip()
        return self._items_cache[tm_code]

    async def _canonical_tm(self, tm_code: str) -> str:
        """Имя марки ТАК, КАК ОНО ЗАПИСАНО В 1С.

        **Агент на это имя влиять не должен.** `build_name` собирает наименование из
        частей `[вид] [марка] [коллекция] [название] [размер]`, и марку он брал из того,
        что прислала модель, — без отката к справочнику. Достаточно было скопировать
        написание из прайса («MOST FLOOR» вместо «Most Flooring»), чтобы переименовать
        сотни позиций разом, и это выглядело бы как обычная нормализация.

        Запретом в промпте такое не лечится: модель копирует написание не по злому
        умыслу, а потому что видит его в прайсе. Поэтому имя не спрашивают — его берут.
        """
        await self._nomenclature(tm_code)
        return self._tm_names.get(tm_code, "")

    async def _items(self, inp: dict) -> str:
        tm_code = str(inp.get("tm_code") or "").strip()
        if not tm_code:
            return "Не передан tm_code."

        items = await self._nomenclature(tm_code)

        # СНЯТЫЕ В ВЫДАЧУ НЕ ИДУТ. Часть коллекций висит под маркой с флагом «Не
        # выгружать» — это УЖЕ снятые, и трогать их не надо: их перенесут в папки снятых
        # отдельно и не сейчас (решение админа 21.09.2026). Увидев их, агент начинал
        # предлагать по ним работу — у Most Flooring так всплыли Quick и Prestige.
        #
        # Фильтруется ВЫДАЧА, а не кеш: сборке правок снятые нужны, иначе при создании
        # позиции она не увидит, что товар уже есть, и заведёт дубль.
        #
        # Поиск по снятым остаётся у `find_1c_items` — он для того и сделан.
        items = [i for i in items if not i.not_exported]

        wanted = (inp.get("collection") or "").strip().lower()
        if wanted:
            items = [i for i in items
                     if wanted in nz.collection_of(i).lower()]

        if not items:
            return ("Среди ЖИВЫХ позиций марки ничего не найдено"
                    + (f" в коллекции «{inp.get('collection')}»." if wanted else ".")
                    + " Снятые сюда не попадают — ищи их через find_1c_items.")

        shown = items[:MAX_ITEMS_SHOWN]
        rows = [self._row(i) for i in shown]
        tail = ("" if len(items) <= MAX_ITEMS_SHOWN else
                f"\n[показано {MAX_ITEMS_SHOWN} из {len(items)} — сузь фильтром collection]")
        return json.dumps(rows, ensure_ascii=False) + tail

    def _row(self, i) -> dict:
        """Одна позиция 1С. СОСТАВ ЗАВИСИТ ОТ ВИДА ЗАДАЧИ, и это не экономия ради экономии.

        Выгрузка едет в КАЖДЫЙ следующий запрос к модели (цикл ручной, история целиком).
        На марке в 900 позиций справочные поля стоят десятки тысяч токенов за шаг — и не
        влияют ни на одно решение о цене. Ровно та же развилка сделана в прайсовом потоке
        (`PricingTools._item_fields`) и по той же причине.
        """
        # КОЛЛЕКЦИЯ ОТДАЁТСЯ ГОТОВОЙ К ПОДСТАНОВКЕ В ИМЯ. Свойство «Коллекция» бывает
        # пустым (у A+ Floor — у всех позиций), и тогда остаётся имя папки, а в нём по
        # §19.5 стоит РАЗМЕР. Отдай мы его как есть — модель подставит его в `collection`
        # и получит «Ламинат A+ Floor Ле Паркет 600x600x14 Авила»: размер в середине
        # наименования, где ему не место. Ровно это и случилось при нормализации.
        row = {
            "ref": i.ref, "name": i.name, "article": i.article, "size": i.size,
            "collection": nz.collection_of(i), "collection_ref": i.collection_ref,
            "product_type": i.product_type, "unit": i.unit, "alt_units": i.alt_units,
            "purchase": i.purchase.value if i.purchase else None,
            "retail": i.retail.value if i.retail else None,
            "rrc": i.rrc.value if i.rrc else None,
        }
        if self._kind == TaskKind.CHANGE_PRICES:
            return row

        row.update({
            "full_name": i.full_name, "site_name": i.site_name,
            "product_type_ref": i.product_type_ref,
            "collection_code": i.collection_code,
            "length_from": i.length_from, "length_to": i.length_to,
            "width_from": i.width_from, "width_to": i.width_to,
            "thickness": i.thickness,
            # Невыгружаемая ветка — это и есть снятые с производства (§19.3).
            "not_exported": i.not_exported,
            "properties": [{"property": p.property, "code": p.code,
                            "value": p.value, "value_code": p.value_code}
                           for p in i.properties],
        })
        return row

    async def _find(self, inp: dict) -> str:
        articles = [str(a).strip() for a in (inp.get("articles") or []) if str(a).strip()]
        name = (inp.get("name") or "").strip()
        if not articles and not name:
            return "Нужен articles (список) либо name."

        # Потолок держим и здесь: 1С откажет на сотне с лишним, а узнать об этом лучше до
        # похода в неё — ответ с ошибкой стоит того же круга цикла, что и полезный.
        if len(articles) > 100:
            return f"Больше 100 артикулов за раз ({len(articles)}) — разбей на части."

        found = await asyncio.to_thread(self._onec.find_items,
                                        articles=articles, name=name)
        return json.dumps({
            "total": found.total,
            # «Не нашлось» и «не поместилось» — разные ответы, и решать по ним надо
            # по-разному: во втором случае вывод «дубля нет» был бы неверен.
            "truncated": found.truncated,
            "items": [{
                "ref": i.ref, "name": i.name, "article": i.article,
                "tm": i.tm, "tm_code": i.tm_code,
                "folder_ref": i.parent_ref, "folder": i.parent_name,
                # Главный признак: невыгружаемая ветка — это и есть снятые с производства
                "not_exported": i.not_exported,
            } for i in found.items],
        }, ensure_ascii=False)

    async def _folders(self, inp: dict) -> str:
        tm_code = str(inp.get("tm_code") or "").strip()
        if not tm_code:
            return "Не передан tm_code."
        tree = await asyncio.to_thread(self._onec.folders, tm=tm_code)
        return json.dumps({
            "total": tree.total,
            "folders": [{"ref": f.ref, "name": f.name, "parent_ref": f.parent_ref,
                         "kind": f.kind, "level": f.level,
                         "not_exported": f.not_exported,
                         "product_type_ref": f.product_type_ref}
                        for f in tree.items],
        }, ensure_ascii=False)

    async def _properties(self, inp: dict) -> str:
        product_type = str(inp.get("product_type") or "").strip()
        if not product_type:
            return "Не передан product_type."
        cat = await asyncio.to_thread(self._onec.properties_by_type, product_type)
        return json.dumps({
            "product_type": cat.product_type,
            "product_type_ref": cat.product_type_ref,
            "properties": [{"property": p.property, "code": p.code,
                            "values": [{"value": v.value, "code": v.code}
                                       for v in p.values]}
                           for p in cat.properties],
        }, ensure_ascii=False)

    # ---------------------------------------------------------------- запись

    async def _write_items(self, inp: dict) -> str:
        tm_code = str(inp.get("tm_code") or "").strip()
        if not tm_code:
            return "Не передан tm_code — без кода марки правку собрать нельзя."

        current = await self._nomenclature(tm_code)

        # ИМЯ МАРКИ ПОДМЕНЯЕТСЯ НА КАНОН ИЗ 1С, что бы ни прислала модель. Расхождение
        # при этом НЕ замалчивается: оно уезжает в ответ инструмента и оттуда в отчёт
        # админу — переименование марки решает человек, а не прогон нормализации.
        canonical = await self._canonical_tm(tm_code)
        asked = str(inp.get("tm_name") or "").strip()
        inp = dict(inp)
        note = ""
        if canonical:
            inp["tm_name"] = canonical
            if asked and asked.casefold() != canonical.casefold():
                note = (f"\n⚠️ Марка в 1С называется «{canonical}», ты передал «{asked}». "
                        f"В наименования пошёл вариант 1С. Если написание надо менять — "
                        f"это отдельное решение админа, скажи о нём в finish.")

        # СВОЙСТВО «Коллекция» НЕ ЗАПОЛНЯЕТСЯ АВТОМАТИЧЕСКИ. Выгрузка отдаёт коллекцию
        # уже выведенной (из папки, если реквизит пуст), и модель, добросовестно увидев
        # её, может вернуть то же значение свойством. Имя папки для этого не годится:
        # оно несёт размер и меняется при пересортировке справочника.
        inp, dropped = nz.strip_collection_property(inp)
        if dropped:
            note += f"\n⚠️ {dropped}."

        # Правила сборки — общие с прайсовым потоком: имена, категории, что считать
        # значимой правкой. Свой второй набор разошёлся бы с первым молча.
        plan = item_rules.plan_collection(inp, current, self._scope)
        ops = plan.ops()
        summary = item_rules.render(plan) + note

        if not ops:
            return summary + "\n\n[Записывать нечего — расхождений не нашлось.]"

        # ПРОВЕРКА ПРАВА — ЗДЕСЬ, вплотную к записи. Между сборкой плана и вызовом 1С
        # прогон могли отменить: истёк захват, админ снял его, прайс уничтожили.
        self._guard()

        result = await asyncio.to_thread(self._onec.set_items, ops)
        # Выгрузка устарела: мы только что завели папки, позиции и значения свойств.
        self._items_cache.pop(tm_code, None)

        created = int(result.get("created") or 0)
        updated = int(result.get("updated") or 0)
        errors = result.get("errors") or []
        self.written_items += created + updated
        self.failed += len(errors)
        for err in errors[:20]:
            self.errors.append(f"{err.get('ref') or err.get('index')}: "
                               f"{err.get('code')} {err.get('message')}")

        report = f"Записано в 1С: создано {created}, изменено {updated}."
        if errors:
            report += (f" НЕ ЗАПИСАНО {len(errors)}:\n"
                       + "\n".join(f"— {e}" for e in self.errors[-len(errors):]))
        return summary + "\n\n" + report

    async def _write_prices(self, inp: dict) -> str:
        tm_code = str(inp.get("tm_code") or "").strip()
        collection = (inp.get("collection") or "").strip()
        if not tm_code or not collection:
            return "Нужны tm_code и collection."

        current = await self._nomenclature(tm_code)
        in_collection = [i for i in current
                         if (i.collection or "").strip().lower() == collection.lower()]
        if not in_collection:
            return (f"В 1С нет коллекции «{collection}» у этой марки. Проверь имя через "
                    "get_1c_items — цены писать некуда.")

        tm_name = (inp.get("tm_name") or "").strip()
        rows = inp.get("items") or []

        if rows:
            group, missing = plan_items(in_collection, tm_code, tm_name, rows, date.today())
        else:
            purchase = inp.get("purchase")
            if purchase is None:
                return "Нужна purchase на коллекцию либо items с ценами по товарам."
            # Ссылку на папку и имя коллекции функция берёт из САМИХ позиций 1С, а не из
            # того, что прислала модель: писать цену по её представлению о ссылке значило
            # бы верить ей больше, чем справочнику.
            group = plan_price_group(in_collection, tm_code, tm_name,
                                     Decimal(str(purchase)), None, date.today())
            missing = []

        payload = build_payload([group])
        if not payload:
            note = "Изменений нет: цены совпадают с текущими либо разница меньше 2%."
            if missing:
                note += f" Не нашлось в 1С: {', '.join(missing)}."
            return note

        self._guard()

        result = await asyncio.to_thread(self._onec.set_prices, payload)

        updated = int(result.get("updated") or 0)
        unchanged = int(result.get("unchanged") or 0)
        errors = result.get("errors") or []
        self.written_prices += updated
        self.failed += len(errors)
        for err in errors[:20]:
            self.errors.append(f"{err.get('ref')}: {err.get('code')} {err.get('message')}")

        report = f"Цены записаны: обновлено {updated}, без изменений {unchanged}."
        if missing:
            report += f" Не нашлось в 1С: {', '.join(missing)}."
        if errors:
            report += (f" НЕ ЗАПИСАНО {len(errors)}:\n"
                       + "\n".join(f"— {e}" for e in self.errors[-len(errors):]))
        return report

    # ----------------------------------------------------------------- итог

    def _finish(self, inp: dict) -> str:
        raw = (inp.get("status") or "").strip().lower()
        status = next((s for s in TaskStatus if s.value == raw), None)
        if status is None:
            return ("Неизвестный статус. Допустимо: "
                    + ", ".join(s.value for s in TaskStatus))

        result = (inp.get("result") or "").strip()
        if not result:
            return "Пустой result — админ должен прочитать, что сделано."

        # «Частично» без причины — бесполезный ответ: админ не узнает, что доделывать.
        if status == TaskStatus.PARTIAL and len(result) < 20:
            return ("Исход «частично обработана» требует причины и того, что именно "
                    "сделано. Напиши подробнее.")

        # «Выполнена», когда в 1С НИЧЕГО не записано и ошибок не было, — подозрительно,
        # но законно: задача могла оказаться уже сделанной. Пусть скажет это словами.
        self.outcome = (status, result)
        return f"Принято: {status.value}."


PROMPT = """Ты — контент-менеджер интернет-магазина напольных покрытий. Тебе дали ОДНУ
задачу по прайсу поставщика, и админ уже подтвердил её запуск. Твоя работа — выполнить её
и доложить, что получилось.

ОПИСАНИЕ ЗАДАЧИ МОГ ДОПОЛНИТЬ АДМИН, и его слова старше твоих. Текст в «Что требуется»
писал агент — возможно, ты сам на прошлом прогоне, — а админ правит его прямо перед
запуском, обычно дописывая снизу. Поэтому:

— Есть в описании вопрос, а ниже ответ на него — действуй по ОТВЕТУ. Вопрос считай
  закрытым и заново его не поднимай.
— Указание админа противоречит рассуждению выше — выполняй указание. Оно новее, и он
  видел больше, чем видел ты.
— Переспросить посреди работы ты не можешь: связи с админом нет, есть только `finish`.
  Значит решай тем, что дано, а сомнение неси в отчёт.

ТЫ ПИШЕШЬ В БОЕВУЮ 1С. `write_items` и `write_prices` записывают немедленно, без второго
подтверждения. Поэтому:

— Сначала ПОСМОТРИ, потом пиши. Прочитай прайс и выгрузку 1С, сопоставь, и только затем
  записывай.
— Пиши ТОЛЬКО то, что относится к этой задаче. Увидел соседнее расхождение — скажи о нём
  в `finish` ОДНОЙ СТРОКОЙ и не трогай: админ его не запускал.

— Отчёт краткий. Админ читает исход, а не ход работы: что изменено в 1С, числами. Ни
  пересказа проверок, ни обоснования решения, ни сводки по прайсу целиком.
— Не выдумывай данные. Нет в прайсе размера — не пиши размер.

— СНЯТОЕ НЕ ТРОГАЙ И НЕ ОБСУЖДАЙ. Позиции и коллекции с флагом «Не выгружать» уже сняты
  с производства; в живую выгрузку они не попадают, и работы по ним нет. Заметил такую в
  `find_1c_items` — учти при проверке на дубль и молчи: предлагать по ней ничего не надо.

Порядок работы:

1. `read_price` — найди в прайсе раздел этой марки и коллекции.
2. `get_1c_items` с tm_code — посмотри, что в 1С уже есть. ОДИН вызов на марку.
3. Для создания позиций: `find_1c_items` со списком артикулов — проверь, нет ли товара
   среди СНЯТЫХ или под другой маркой. Иначе заведёшь дубль. Один вызов на коллекцию.
4. `get_1c_folders` — куда класть; `get_1c_properties` — коды значений свойств.
5. `write_items` и/или `write_prices` — по одной коллекции за вызов.
6. `finish` — исход и что сделано числами.

ВИДЫ ЗАДАЧ:

— «нормализация наименований» — привести имена к правилам: `write_items` с op=update,
  передавай `title` (расцветка) и `tail` (размер), остальное соберёт код;
— «изменение свойств» — размеры, ЕИ, коэффициент упаковки, свойства вида товара;
— «перенос в снятые» — `write_items` с op=update и новым `parent_ref` папки снятых;
— «добавление новых» — op=create, ОБЯЗАТЕЛЬНО после `find_1c_items`;
— «изменение цен» — `write_prices`.

ЭКОНОМЬ ВЫЗОВЫ. Каждый вызов инструмента — отдельный запрос к модели со всей историей;
выгрузка 1С весит тысячи токенов и едет в каждом следующем. Не запрашивай одно и то же
дважды: если выгрузка уже приходила, работай с ней, а не проси заново.

ОТЧЁТ ЧЕСТНЫЙ. «Выполнена» — сделано всё задуманное. «Частично обработана» — часть не
удалась, и тогда в result обязаны стоять ПРИЧИНА и что именно сделано; это нормальный
исход, а не провал. Не получилось ничего — «к обработке», задача вернётся в очередь.
Не приписывай себе того, чего не делал: админ видит только твой текст."""


async def run_normalization(onec, task, guard, scope=None):
    """Нормализация наименований кодом (§6.2, вариант «без LLM»).

    Модель не участвует: вход для `plan_collection` собирается из полей 1С, сборка имени —
    существующие правила §19.5. Отсюда и свойства: ноль токенов, секунды вместо минут и
    один и тот же результат при повторе.
    """
    from src.model import normalize as nz

    tm_code = (task.address.tm.code or "").strip()
    if not tm_code:
        return (TaskStatus.TODO,
                "У задачи нет кода марки 1С — нормализовать нечего. Пересоберите задачи "
                "или укажите марку.")

    nom = await asyncio.to_thread(onec.by_tm_all, tm_code, include_not_exported=True)
    tm_name = (nom.tm or "").strip()
    # Коллекция берётся из адреса задачи: она чаще всего адресована одной, и сужать
    # первую настоящую запись до проверяемой глазами — правильно по умолчанию.
    only = task.address.subject.label() if task.subject == TaskSubject.COLLECTION else ""

    inputs, skipped, discontinued = nz.plan(nom.items, tm_code, tm_name,
                                            only_collection=only)

    if not inputs and not skipped:
        return (TaskStatus.DONE,
                f"В 1С не нашлось позиций для нормализации"
                + (f" в коллекции «{only}»." if only else " по этой марке.")
                + (f" Снятых с производства: {discontinued}." if discontinued else ""))

    written = 0
    failed: list[str] = []

    for inp in inputs:
        plan = item_rules.plan_collection(inp, list(nom.items), list(scope or []))
        ops = plan.ops()
        if not ops:
            continue

        # Право на запись — вплотную перед КАЖДЫМ вызовом 1С, ровно как в LLM-пути:
        # коллекций может быть много, прогон длится, а захват за это время теряется.
        guard()

        result = await asyncio.to_thread(onec.set_items, ops)
        written += int(result.get("updated") or 0)
        for err in (result.get("errors") or [])[:20]:
            failed.append(f"{err.get('ref') or err.get('index')}: "
                          f"{err.get('code')} {err.get('message')}")

    text = nz.report(written, skipped, discontinued, len(inputs))
    if failed:
        text += ("\n\nНЕ ЗАПИСАНО " + str(len(failed)) + ":\n"
                 + "\n".join(f"— {f}" for f in failed))

    # ЧАСТИЧНО — когда часть работы осталась человеку либо не записалась. Это штатный
    # исход, и статус обязан его называть: «выполнена» скрыла бы список на разбор.
    status = TaskStatus.PARTIAL if (skipped or failed) else TaskStatus.DONE
    return status, text


async def run_discontinue(onec, task, guard):
    """Перенос ЦЕЛОЙ коллекции в снятые — кодом, одним вызовом (§6.2).

    **ПЕРЕНОСИМ ПАПКУ, А НЕ ПОЗИЦИИ.** Коллекция ушла из прайса целиком, значит и в снятые
    она уходит целиком: одна операция `update_folder` вместо N штук `update_item`. Агент,
    не имея в инструментах переноса папки, двигал позиции по одной — восемь вызовов там,
    где хватало одного, и пустая папка осталась висеть в живых.

    **РЕШАТЬ ЗДЕСЬ НЕЧЕГО**, поэтому и модели здесь нет: папка известна из самих позиций,
    целевая папка снятых — из вида товара (`discontinued.folder_for`), а она же выбирает
    ПРОФИЛЬНУЮ папку, а не общую свалку.
    """
    from src.price_tool import discontinued as dc

    tm_code = (task.address.tm.code or "").strip()
    wanted = task.address.subject.label()
    if not tm_code or not wanted:
        return (TaskStatus.TODO,
                "У задачи нет кода марки или имени коллекции — переносить нечего.")

    nom = await asyncio.to_thread(onec.by_tm_all, tm_code, include_not_exported=True)
    mine = [i for i in nom.items
            if nz.collection_of(i).casefold() == wanted.casefold()]
    live = [i for i in mine if not i.not_exported]

    # ПАПКУ ИЩЕМ В ДЕРЕВЕ, А НЕ ТОЛЬКО ПО ПОЗИЦИЯМ. Позиции коллекции могли уже уехать в
    # снятые по одной — так и вышло с Brilliant, — и тогда `collection_ref` у них ведёт
    # уже не сюда, а папка остаётся висеть в живой ветке пустой. Именно её и надо убрать.
    tree = await asyncio.to_thread(onec.folders, tm=tm_code)
    folder = _collection_folder(tree.items, task.address.subject, wanted)

    if folder is None:
        return (TaskStatus.TODO,
                f"Папка коллекции «{wanted}» у этой марки не нашлась — перенести нечего. "
                "Возможно, позиции лежат прямо в папке марки.")

    if folder.not_exported:
        return (TaskStatus.DONE,
                f"Папка «{folder.name}» уже помечена невыгружаемой — коллекция снята.")

    # ПАПКА ДОЛЖНА БЫТЬ НАША ЦЕЛИКОМ: перенос утащит всё, что внутри.
    strangers = sorted({nz.collection_of(i) for i in nom.items
                        if i.collection_ref == folder.ref
                        and nz.collection_of(i).casefold() != wanted.casefold()})
    if strangers:
        return (TaskStatus.TODO,
                f"В папке «{folder.name}» лежат и другие коллекции "
                f"({', '.join(strangers[:5])}) — перенос утащил бы их следом. "
                "Разберите папку либо перенесите позиции по одной.")

    # Вид товара берём у позиций коллекции; если их не осталось — у папки.
    type_ref = (mine[0].product_type_ref if mine else folder.product_type_ref)
    target = dc.folder_for(type_ref)
    if not target:
        return (TaskStatus.TODO,
                dc.refusal(type_ref, mine[0].product_type if mine else ""))

    guard()
    result = await asyncio.to_thread(
        onec.set_items,
        [{"op": "update_folder", "ref": folder.ref, "parent_ref": target}])

    errors = result.get("errors") or []
    if errors:
        return (TaskStatus.TODO,
                f"Папку «{folder.name}» перенести не удалось: "
                + "; ".join(f"{e.get('code')} {e.get('message')}" for e in errors[:3]))

    text = (f"Папка «{folder.name}» перенесена в снятые ({target}) целиком, "
            f"вместе с ней {len(live)} позиц. Наименования и цены не менялись.")
    gone = len(mine) - len(live)
    if gone:
        text += (f"\nЕщё {gone} позиц. этой коллекции были сняты раньше и лежат в другой "
                 "папке снятых — их не трогал.")
    return TaskStatus.DONE, text


def _collection_folder(folders, subject, wanted: str):
    """Папка коллекции в дереве марки. None — не нашлась.

    Сперва по КОДУ из адреса задачи: его кладёт `_add_discontinued_candidates`, и это
    точное попадание без всякого сопоставления имён. Имена папок в базе разнородны —
    «Коллекция Brilliant - 10 декоров» рядом с «Provence», — и угадывать по ним значит
    однажды перенести не ту.

    Кода нет (задача заведена прежней версией) — ищем по имени как по СЛОВУ: «Brilliant»
    внутри «Коллекция Brilliant - 10 декоров» есть, а «Accord» внутри «Accord Plus» —
    другое слово. Нашлось несколько — не двигаем ничего.
    """
    from src.price_tool.scope import normalize

    by_ref = {f.ref: f for f in folders}
    code = (subject.code or "").strip()
    if code and code in by_ref:
        return by_ref[code]

    probe = normalize(wanted)
    if not probe:
        return None

    hits = [f for f in folders
            if f.kind == "collection" and probe in normalize(f.name).split()]
    # Составное имя («ECO plus») одним словом не найти — пробуем вхождением целиком.
    if not hits:
        hits = [f for f in folders
                if f.kind == "collection" and probe in normalize(f.name)]
    return hits[0] if len(hits) == 1 else None


def task_brief(price, task) -> str:
    """Что именно предстоит сделать — одним куском для модели."""
    lines = [
        f"ЗАДАЧА №{task.id}: {task.kind.value}",
        f"Предмет: {task.address.label()} ({task.subject.value})",
        f"Прайс: «{price.supplier_price.filename}» (№{price.id})",
    ]
    tm = task.address.tm
    if tm.code:
        lines.append(f"Код марки в 1С: {tm.code}")
    subject = task.address.subject
    if subject.article:
        lines.append(f"Артикул: {subject.article}")
    if task.description:
        lines.append(f"\nЧто требуется:\n{task.description}")
    # ПРОШЛЫЙ РЕЗУЛЬТАТ ВАЖЕН: задачу запускают повторно именно тогда, когда в прошлый
    # раз получилось не всё, и агент должен доделать остаток, а не начать сначала.
    if task.result:
        lines.append(f"\nПрошлый результат (задача запускается повторно):\n{task.result}")
    return "\n".join(lines)


async def run(orchestrator, onec, price, task, content: bytes, guard,
              scope=None, usage_labels: dict | None = None):
    """Выполнить задачу. Возвращает (статус, текст результата).

    `guard` — функция без аргументов, бросающая `WriteRefused`, если прогон потерял право
    писать. Зовётся перед каждой записью в 1С.

    Молчаливый агент не имеет права выдать себя за успех: не вызвал `finish` — задача
    остаётся «к обработке» с пояснением, а не помечается выполненной.
    """
    # НОРМАЛИЗАЦИЯ ИДЁТ МИМО МОДЕЛИ. У неё нет ни одного входа снаружи 1С — прайс для
    # неё не нужен вовсе, — а значит и решать нечего: имя собирается по шаблону из полей
    # самой карточки. Платить за круги цикла и терпеть непредсказуемость незачем.
    if task.kind == TaskKind.NORMALIZE_NAMES:
        return await run_normalization(onec, task, guard, scope=scope)

    # ПЕРЕНОС ЦЕЛОЙ КОЛЛЕКЦИИ — тоже мимо модели: папка известна из позиций, целевая
    # папка снятых — из вида товара. Одна операция вместо N, и рассуждать не о чем.
    # Задача про ОДИН товар остаётся агенту: там надо понять, что именно снимают.
    if (task.kind == TaskKind.MOVE_DISCONTINUED
            and task.subject == TaskSubject.COLLECTION):
        return await run_discontinue(onec, task, guard)

    tools = TaskTools(onec, content, price.supplier_price.filename, guard,
                      scope=scope, kind=task.kind)

    answer, _ = await orchestrator.handle_turn(
        [{"role": "user", "content": task_brief(price, task)}],
        system=PROMPT, extra_tools=TOOLS, extra_executor=tools,
        base_tools=False, usage_labels=usage_labels)

    if tools.outcome is not None:
        return tools.outcome

    # `finish` не вызван. Если в 1С что-то записано — это уже не «ничего не делали», и
    # прятать запись за статусом «к обработке» нельзя: админ повторит задачу и запишет
    # второй раз.
    written = tools.written_items + tools.written_prices
    if written:
        return (TaskStatus.PARTIAL,
                f"Агент не доложил исход, но запись в 1С состоялась: позиций "
                f"{tools.written_items}, цен {tools.written_prices}. "
                f"Проверьте результат в 1С. Ответ агента: {answer[:500]}")

    return (TaskStatus.TODO,
            f"Агент не доложил исход и в 1С ничего не записал. "
            f"Ответ агента: {answer[:500]}")
