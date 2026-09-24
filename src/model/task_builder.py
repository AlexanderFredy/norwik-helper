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

Чего сверка не даёт даже так: самих наименований 1С. Поэтому агент по-прежнему не судит о
соответствии имён шаблону — это делает код при выполнении.

Цены сверяются, но тоже КОДОМ (`src/model/price_check.py`): агент называет колонки листа,
а числа сравнивает модуль. Ни одной цены 1С в контекст при этом не уезжает, зато задача
«изменение цен» перестала заводиться там, где менять нечего.

**Инструменты возвращают текст, а состояние меняет код.** Задачи копятся в `collected` и
попадают в модель одним куском после хода: так неудачный ход не оставляет половину списка.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from src.model.enums import TaskKind, TaskSubject
from src.model.normalize import collection_of
from src.model.refs import Ref, TaskAddress, norm_article
from src.model.task import PriceTask
from src.model import price_check
from src.price_tool.items import build_name
from src.price_tool.parser import parse_price_table, render_preview
from src.price_tool.scope import normalize

logger = logging.getLogger(__name__)

MAX_SHEET_ROWS = 200        # строк листа в один ответ инструмента
MAX_TASKS = 200             # потолок на прогон: защита от разгона, а не рабочий предел


def _price_notes_only(text: str | None) -> str:
    """Оставить в тексте агента ТОЛЬКО наблюдения по прайсу, убрав повтор стандарта.

    Агент, которому велено «не описывай шаблон», всё равно начинает с «Привести
    наименования марки к шаблону §19.5» — и описание выходит с двумя одинаковыми
    предложениями подряд, своим и его. Ему это не в укор: он пишет описание задачи, а то,
    что стандарт допишет код, для него невидимо.

    Режется ПО ПРЕДЛОЖЕНИЯМ и только те, где он пересказывает канон: упоминание §19.5 или
    «привести … к шаблону». Всё остальное — про прайс — остаётся нетронутым.
    """
    raw = (text or "").strip()
    if not raw:
        return ""

    kept = []
    for part in re.split(r"(?<=[.!?])\s+", raw):
        probe = part.lower()
        if "19.5" in probe or ("шаблон" in probe and "привести" in probe):
            continue
        kept.append(part)

    out = " ".join(kept).strip()
    # Осталась одна связка вроде «Наблюдения по прайсу:» — смысла в ней нет.
    return "" if len(out) < 15 else out


def _revival_brief(items) -> str:
    """Текст про возврат из снятых: коды 1С и папка, где позиции лежат сейчас.

    Пишется КОДОМ по ответу 1С — админ и исполняющий агент читают тут идентификаторы, а
    не пересказ. Формулировка прямая («вернуть», а не «проверить»): это уже проверено
    запросом, решать нечего.
    """
    lines = [f"— {i.article} «{i.name}» — код 1С {i.ref}"
             + (f", сейчас в «{i.parent_name}»" if i.parent_name else "")
             for i in items[:40]]
    tail = f"\n…и ещё {len(items) - 40}" if len(items) > 40 else ""
    return ("ВОЗВРАТ РАНЕЕ СНЯТОГО, не заведение заново. Эти позиции в 1С УЖЕ ЕСТЬ и лежат "
            "вне живой коллекции — создавать их повторно нельзя, получится дубль с "
            "потерянной историей цен. Вернуть сменой родителя на папку живой коллекции:\n"
            + "\n".join(lines) + tail)


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
            "СПИСОК артикулов, которые ты увидел в этой коллекции прайса. АРТИКУЛЫ "
            "ОБЯЗАТЕЛЬНЫ: сопоставление идёт по ним, а не по имени.\n"
            "Вернётся КОРОТКАЯ сводка, и в ней `collection_в_1С` — КАК ЭТА ЖЕ КОЛЛЕКЦИЯ "
            "НАЗЫВАЕТСЯ В СПРАВОЧНИКЕ. Имена расходятся штатно: в прайсе «Миллениум Про», "
            "в 1С «Millenium Pro». Адресуй задачу именем из 1С.\n"
            "ЗОВИ ЭТО ПЕРЕД add_task по каждой коллекции. Без сверки ты можешь написать "
            "только «проверить», а со сверкой — «завести 3 недостающих: …». Выгрузка "
            "номенклатуры при этом в ответ НЕ попадает: сравнение делает код.\n"
            "ЦЕНЫ — два способа, выбирай по прайсу.\n"
            "1) Цена у каждой позиции своя: передай `price_columns` — где артикул, где "
            "закупка, где РРЦ (номер колонки с единицы либо кусок заголовка). Достаточно "
            "одного раза на лист.\n"
            "2) Цена ОДНА НА КОЛЛЕКЦИЮ (так пишут часто: цифры стоят в строке коллекции, "
            "у остальных декоров ячейки пустые): передай `prices` — {purchase, rrc}.\n"
            "Сравнивает код и отвечает полем `цены`: что расходится, сколько позиций уже "
            "стоят как в прайсе, у скольких цены в 1С нет вовсе. Сами цены 1С в ответ не "
            "попадают."),
        "input_schema": {
            "type": "object",
            "properties": {
                "tm_code": {"type": "string"},
                "collection": {"type": "string"},
                "articles": {"type": "array", "items": {"type": "string"}},
                "prices": {
                    "type": "object",
                    "description": ("цена ОДНА НА ВСЮ коллекцию, если в прайсе она "
                                    "написана так: {purchase, rrc}"),
                    "properties": {
                        "purchase": {"type": ["string", "number"]},
                        "rrc": {"type": ["string", "number"]},
                    },
                    "additionalProperties": False,
                },
                "price_columns": {
                    "type": "object",
                    "description": ("где в листе артикул и цены: номер колонки с единицы "
                                    "либо кусок её заголовка"),
                    "properties": {
                        "article": {"type": ["string", "integer"]},
                        "purchase": {"type": ["string", "integer"]},
                        "rrc": {"type": ["string", "integer"]},
                        "sheet": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
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

    def __init__(self, content: bytes, filename: str, onec=None,
                 elsewhere: dict | None = None) -> None:
        self._content = content
        self._filename = filename
        self._onec = onec
        # Артикул → где его видели у ДРУГИХ поставщиков (`storage/sightings.py`). Снимок
        # берётся один раз перед ходом: спрашивать базу из синхронного кода инструментов
        # неоткуда, а таблица мала.
        self._elsewhere = elsewhere or {}
        # Что этот прайс показал: артикул → коллекция, как она названа В ПРАЙСЕ. Ляжет в
        # тот же журнал после прогона — из него и узнает о коллекции следующий прайс.
        self.seen_articles: dict[str, str] = {}
        # Что этот поставщик просит: артикул → {purchase, rrc}. Ложится в тот же журнал
        # и потом решает, чью цену писать в 1С (§6.4): наименьшую среди свежих.
        self.seen_prices: dict[str, dict] = {}
        # Коллекция (нормализованно) → позиции, которые в 1С УЖЕ ЕСТЬ, но вне живой папки:
        # чаще всего снятые. Заводить их заново нельзя — это дубль.
        self._revivals: dict[str, list] = {}
        # Имя коллекции → марки-владельцы по дереву папок 1С. Считается один раз.
        self._owners: dict | None = None
        self._haystack: str | None = None   # весь текст прайса одной строкой
        self._seen_trees: set = set()   # марки, чьё дерево папок уже спрашивали
        self._collection_name = ""      # коллекция текущей сверки — для поиска владельца
        # Последняя ошибка поиска по всей номенклатуре. Молчать о ней нельзя: сорвавшийся
        # поиск ВЫГЛЯДИТ как «ничего не нашлось», и вывод «коллекция новая» становится
        # ложью — 22.09.2026 именно так и вышло, когда 1С отдавала 500 на любой артикул.
        self.search_error = ""
        self.collected: list[PriceTask] = []
        self.marks: list[dict] = []
        self._sheets = None
        self._items_cache: dict[str, list] = {}
        self._tm_names: dict[str, str] = {}     # код марки → её имя В 1С
        self._touched: dict[str, set] = {}      # код марки → коллекции 1С, закрытые сверкой
        self._last_sheet: str = ""              # лист, который агент читал последним
        self._price_cols: dict[str, dict] = {}  # имя листа → названные колонки цен

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

        self._last_sheet = sheet.name
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

    def untouched_collections(self) -> dict[str, list[str]]:
        """Живые коллекции 1С, которых прайс не закрыл ВОВСЕ. Код марки → имена.

        **ЗАЧЕМ ЭТО ОТДЕЛЬНО.** `compare_with_1c` идёт ОТ ПРАЙСА: агент называет коллекцию,
        которую там увидел. Коллекция, существующая только в 1С, в этот обход не попадает
        никогда — её просто не о чем спрашивать. Так у Most Flooring осталась незамеченной
        Brilliant: восемь живых позиций, которых в прайсе нет, и никто о них не сказал.

        Считается ПОСЛЕ хода, по следу вызовов сверки: какие коллекции агент закрыл, такие
        и вычитаем. Модель не участвует — она и не может, у неё нет списка коллекций 1С.
        """
        out: dict[str, list[str]] = {}
        for tm_code, items in self._items_cache.items():
            covered = self._touched.get(tm_code, set())
            by_collection: dict[str, list[str]] = {}
            for i in items:
                if i.not_exported:
                    continue
                name = collection_of(i)
                if name:
                    by_collection.setdefault(name, []).append(i.article or "")

            rest = []
            for name, articles in sorted(by_collection.items()):
                if name in covered:
                    continue
                # «АГЕНТ НЕ СВЕРЯЛ» — НЕ «В ПРАЙСЕ НЕТ». Прайс бывает на несколько листов,
                # агент обошёл не все, и коллекции со второго листа выглядели снятыми:
                # Westerhof так получил задачи на снятие COSMO, Shine и Aristocrat, живых
                # и стоящих в файле (бой 24.09.2026). Поэтому спрашиваем САМ ФАЙЛ.
                if self._in_price(name, articles):
                    continue
                rest.append(name)

            if rest:
                out[tm_code] = rest
        return out

    def _nameless_twin(self, collection: str, live: list) -> dict | None:
        """Коллекция 1С с ТЕМ ЖЕ именем, чьи позиции не имеют артикулов.

        Возвращает сводку с доказательствами: сколько позиций, у скольких пуст артикул и
        в какой строке прайса встречается название расцветки. По этим строкам артикул
        проставляется — это и есть настоящая работа, а не заведение коллекции заново.
        """
        keys = _collection_keys(collection)
        members = [i for i in live if _collection_keys(collection_of(i)) & keys]
        if not members:
            return None

        nameless = [i for i in members if not (i.article or "").strip()]
        if not nameless:
            return None

        pairs = []
        for item in nameless[:10]:
            line = self._price_line(item.site_name or item.name)
            pairs.append({"ref": item.ref,
                          "расцветка": item.site_name or item.name,
                          "строка_прайса": line or "не нашлась"})

        return {
            "коллекция": collection_of(members[0]),
            "папка": members[0].collection_ref,
            "позиций": len(members),
            "без_артикула": len(nameless),
            "вывод": ("коллекция В 1С ЕСТЬ, заводить заново НЕЛЬЗЯ — будут дубли. "
                      "Сверить по артикулу её нельзя, пока он пуст: заведи задачу "
                      "«изменение свойств» на простановку артикулов по строкам ниже"),
            "позиции": pairs,
        }

    def _price_line(self, title: str) -> str:
        """Первая строка прайса, где встречается название расцветки. Пусто — не нашлась."""
        flat = "".join(ch for ch in str(title or "").lower() if ch.isalnum())
        if len(flat) < 4:
            return ""
        for sheet in self.sheets:
            for row in sheet.rows:
                text = " ".join(str(c) for c in row if str(c or "").strip())
                if flat in "".join(ch for ch in text.lower() if ch.isalnum()):
                    return " ".join(text.split())[:120]
        return ""

    def mark_coverage(self, tm_code: str) -> tuple[int, int]:
        """Сколько ЖИВЫХ коллекций марки закрыл этот прайс и сколько их всего.

        Нужно, чтобы не предлагать снятие по марке, о которой прайс почти ничего не
        говорит. Выгрузка марки попадает в кеш от ЛЮБОГО вызова сверки — в том числе
        когда код сам переставил марку, найдя артикулы под чужой.
        """
        items = self._items_cache.get(tm_code) or ()
        live = {collection_of(i) for i in items
                if not i.not_exported and collection_of(i)}
        covered = self._touched.get(tm_code, set())
        return len(live & covered), len(live)

    def _in_price(self, collection: str, articles) -> bool:
        """Встречается ли коллекция в прайсе — по артикулам и по имени.

        Ищем по НОРМАЛИЗОВАННОМУ тексту всего файла: артикул в ячейке слеплен с названием
        и размером («…Альфа PELI (CO 512) 1290*190*8мм»), и сравнение «ячейка = артикул»
        не нашло бы ничего. Артикулы короче трёх знаков не проверяем: «12» найдётся в любом
        размере и объявит живой любую коллекцию.

        Имя коллекции проверяется И БЕЗ РАЗМЕРНОГО ХВОСТА: в 1С папка зовётся «Aristocrat
        1200х400», а в прайсе — просто «Aristocrat».
        """
        haystack = self._price_haystack()
        if not haystack:
            return False        # файл не разобрался — решать по нему нечего

        for article in articles or ():
            key = norm_article(article)
            if len(key) >= 3 and key in haystack:
                return True

        for key in _collection_keys(collection):
            flat = "".join(ch for ch in key if ch.isalnum())
            if len(flat) >= 4 and flat in haystack:
                return True
        return False

    def _price_haystack(self) -> str:
        """Весь текст прайса одной строкой, без разделителей и регистра. Считается один
        раз: файл за прогон не меняется, а склейка сотен строк не бесплатна."""
        if self._haystack is None:
            parts = []
            for sheet in self.sheets:
                for row in sheet.rows:
                    for cell in row:
                        text = str(cell or "")
                        if text.strip():
                            parts.append("".join(ch for ch in text.lower()
                                                 if ch.isalnum()))
            self._haystack = " ".join(parts)
        return self._haystack

    def _normalization_pointless(self, tm_code: str) -> str:
        """Отказ, если нормализовать нечего. Пустая строка — задача нужна.

        Сбой сверки задачу НЕ отменяет: не сумев проверить, безопаснее завести — админ
        откроет и увидит «менять было нечего», а вот пропущенная работа не всплывёт никак.
        """
        if self._onec is None or not tm_code:
            return ""
        try:
            from src.model import normalize as nz
            items = self._nomenclature(tm_code)
            if nz.pending_work(items, tm_code, self._tm_name(tm_code)) > 0:
                return ""
        except Exception:                               # noqa: BLE001
            logger.warning("Не удалось оценить нормализацию марки %s", tm_code,
                           exc_info=True)
            return ""

        return ("Нормализация этой марке не нужна: наименования уже собраны по шаблону "
                "§19.5. Задачу не завёл. Если в прайсе есть НАБЛЮДЕНИЯ, о которых стоит "
                "сказать админу, — вынеси их в свой итоговый ответ.")

    def _tm_name(self, tm_code: str) -> str:
        """Имя марки как оно записано в 1С."""
        self._nomenclature(tm_code)
        return self._tm_names.get(tm_code, "")

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

        from_price = []
        seen = set()
        for raw in (inp.get("articles") or []):
            key = norm_article(raw)
            if key and key not in seen:
                seen.add(key)
                from_price.append((key, str(raw).strip()))

        # МАРКУ ОПРЕДЕЛЯЕТ 1С, А НЕ ДОГАДКА МОДЕЛИ.
        self._collection_name = collection
        tm_code, moved = self._actual_mark(tm_code, seen,
                                           [shown for _k, shown in from_price])

        items = self._nomenclature(tm_code)
        live = [i for i in items if not i.not_exported]

        # СОПОСТАВЛЕНИЕ ПО АРТИКУЛУ, А НЕ ПО ИМЕНИ КОЛЛЕКЦИИ.
        #
        # Имена расходятся штатно: у Most Flooring в 1С коллекции названы латиницей
        # («Millenium Pro», «Provence», «High Glossy»), а в прайсе — кириллицей
        # («Миллениум Про», «Прованс», «Супер Глянец»). Поиск по имени не находил ничего,
        # сверка отвечала «коллекции в 1С нет вовсе», и агент завёл СЕМЬ задач «добавить
        # всё» по коллекциям, которые давно заведены. Выполнение дало бы ~56 дублей.
        #
        # Артикул же одинаков в обоих источниках — 3309 и в прайсе 3309, и в 1С. По нему
        # и сопоставляем, а имя коллекции 1С ВОЗВРАЩАЕМ агенту: пусть адресует задачу так,
        # как она называется в справочнике.
        by_article = {norm_article(i.article): i for i in live if i.article}
        found = [by_article[key] for key, _ in from_price if key in by_article]
        missing_in_1c = [shown for key, shown in from_price if key not in by_article]

        # ЧТО ЭТОТ ПРАЙС ВИДЕЛ — в журнал встреч (`storage/sightings.py`). Артикулы уже
        # перечислены моделью, коллекция названа ею же: запись стоит ноль токенов и ноль
        # обращений к 1С. Ради неё и заведён журнал — следующий прайс другого поставщика
        # узнает отсюда, что коллекция жива, и не предложит её снимать.
        for key, _shown in from_price:
            self.seen_articles[key] = collection

        # Куда на самом деле легли найденные артикулы. Обычно одна коллекция; несколько —
        # значит раздел прайса собран из разных, и это тоже надо показать.
        where: dict[str, int] = {}
        for i in found:
            name = collection_of(i)
            where[name] = where.get(name, 0) + 1

        # Обратная сторона: позиции ТЕХ ЖЕ коллекций, которых в прайсе не встретилось, —
        # кандидаты в снятые. Только КАНДИДАТЫ: прайс мог быть частичным.
        gone = [i for i in live
                if collection_of(i) in where and norm_article(i.article) not in seen]
        # …и сразу отсекаем те, что возит КТО-ТО ДРУГОЙ: они не сняты с производства, у
        # них сменился поставщик. Решает это код по журналу встреч, а не рассуждение.
        kept, missing_in_price = [], []
        for i in gone:
            line = f"{i.article} {i.site_name or i.name}".strip()

            # ТА ЖЕ ПРОВЕРКА, ЧТО И У КОЛЛЕКЦИЙ: агент перечислил артикулы одного листа,
            # а позиция стоит на другом. «Он её не назвал» — не «её в прайсе нет».
            if self._in_price("", [i.article]) or self._in_price(i.site_name or "", []):
                continue

            met = self._elsewhere.get(norm_article(i.article))
            if met is None:
                missing_in_price.append(line)
            else:
                kept.append(f"{line} — {met.label()}")

        # Отмечаем, какие коллекции 1С этот раздел прайса закрыл: незакрытые всплывут
        # после хода отдельной проверкой (`untouched_collections`).
        self._touched.setdefault(tm_code, set()).update(where)

        out = {
            "collection_в_прайсе": collection,
            "collection_в_1С": sorted(where) or None,
            "нашлось_в_1С": len(found),
            "прислано_из_прайса": len(from_price),
            "missing_in_1c": missing_in_1c[:40],
            "missing_in_price": missing_in_price[:40],
            "цены": self._price_diff(found, inp),
            "empty_properties": self._empty_properties(
                [i for i in live if collection_of(i) in where]),
            # СВОЙСТВО «Коллекция» СЧИТАЕТСЯ ОТДЕЛЬНО, и вот почему. `empty_properties`
            # видит только те свойства, которые у позиции ЕСТЬ, но без значения: 1С
            # отдаёт лишь проставленные. Свойство, не заводившееся ни разу, в этот список
            # не попадает вовсе — так и осталась незамеченной новая коллекция «Классик»,
            # где его нет ни у одной из восьми позиций.
            #
            # А оно не одно из многих: по нему коллекция опознаётся на сайте, и пустое
            # оно заставляет выводить имя из папки — с размером внутри.
            "без_свойства_Коллекция": sum(
                1 for i in live
                if collection_of(i) in where and not (i.collection or "").strip()),
        }

        # Эти два ключа кладутся, ТОЛЬКО когда им есть что сказать. Пустой список в ответе
        # инструмента — не «ничего», а восемь десятков символов, которые поедут в каждый
        # следующий запрос: цикл ручной, история целиком.
        revival = self._revival_note(missing_in_1c, collection)
        if revival:
            # Уже заведены в 1С, но лежат вне живой папки — почти всегда в снятых.
            # Заводить заново нельзя: будет дубль. Описание допишет код (см. `_add`).
            out["уже_есть_в_1С_вне_коллекции"] = revival
        if kept:
            # Пропали из этого прайса, но возит другой поставщик: снимать НЕ надо.
            out["есть_у_другого_поставщика"] = kept[:40]

        if moved:
            # Марку в адресе задачи надо ставить ЭТУ: иначе задача уедет на марку с
            # обложки прайса, а работа лежит под другой.
            out["марка_в_1С"] = {"tm_code": tm_code, "почему": moved}

        # КОЛЛЕКЦИЯ БЕЗ АРТИКУЛОВ НЕВИДИМА ДЛЯ СВЕРКИ. Сопоставление идёт по артикулу —
        # имена расходятся штатно, — и позиция с пустым артикулом не совпадёт ни с чем. У
        # Вестерхофа так вышло с COSMO: папка YO-00052996 на месте, шесть живых позиций, у
        # всех артикул пуст, и агент предложил завести коллекцию заново — то есть получить
        # девять дублей (бой 24.09.2026). Ищем такую коллекцию ПО ИМЕНИ и говорим прямо.
        if not found:
            blind = self._nameless_twin(collection, live)
            if blind:
                out["коллекция_есть_в_1С"] = blind

        if not found and self.search_error:
            out["note"] = (f"1С НЕ ОТВЕТИЛА на поиск по всей номенклатуре "
                           f"({self.search_error}). Вывод «коллекции нет» НЕ подтверждён: "
                           f"позиции могут лежать под другой маркой. Задачу на заведение "
                           f"всего заново НЕ заводи, скажи об этом в ответе")
        elif not found:
            out["note"] = ("ни один артикул не нашёлся у этой марки — коллекция и правда "
                           "новая ЛИБО артикулы в прайсе записаны иначе; проверь колонку "
                           "артикула, прежде чем заводить всё заново")
        elif len(where) > 1:
            out["note"] = ("артикулы раздела лежат в РАЗНЫХ коллекциях 1С — заводи задачи "
                           "по каждой отдельно")
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

    def _owner_of(self, collection: str, tm_code: str) -> str:
        """Код марки, которой принадлежит папка коллекции. Пусто — не определилось.

        **Дерево запрашивается С ФИЛЬТРОМ ПО МАРКЕ.** Без фильтра обработчик отдаёт только
        верхушку — корни, виды товара и папки снятых, — а папок коллекций в ответе нет
        вовсе (проверено на бою 22.09.2026: 45 узлов, ни одной коллекции). Именно поэтому
        сверка марок после хода ничего не находила: карта владельцев строилась по дереву,
        в котором коллекций нет.

        Дерево каждой марки берётся один раз за прогон: за составление задач оно не
        меняется, а весит прилично.
        """
        if self._onec is None or not (collection or "").strip() or not tm_code:
            return ""
        if self._owners is None:
            self._owners = {}
        if tm_code not in self._seen_trees:
            self._seen_trees.add(tm_code)
            try:
                tree = self._onec.folders(tm=tm_code)
                marks = self.marks or [
                    {"name": m.name, "code": m.code, "selling": m.selling}
                    for m in self._onec.selling_tm(all_marks=True)]
                for key, owners in owner_map(tree.items, marks).items():
                    self._owners.setdefault(key, set()).update(owners)
            except Exception:                           # noqa: BLE001
                logger.warning("Дерево папок марки %s не получено", tm_code,
                               exc_info=True)
        # Однозначного владельца нет («Классик» бывает у двух марок) — выбирать наугад
        # хуже, чем не выбирать.
        owners = self._owners.get(normalize(collection)) or set()
        return next(iter(owners))[0] if len(owners) == 1 else ""

    def _search_1c(self, shown: list[str], keys: set) -> list:
        """Поиск по всей номенклатуре — ТОЛЬКО точные совпадения артикула.

        **`find-items` ищет ВХОЖДЕНИЕМ** (§19.11), и это не недосмотр: он написан, чтобы
        находить товар по куску названия. Но артикулы у ламината короткие, и на запрос
        «301» 1С честно вернула обои «101489301» и «26909302». Без этого фильтра в
        описание задачи уезжали чужие коды 1С — по ним её бы и выполнили.
        """
        if self._onec is None or not shown:
            return []
        try:
            found = self._onec.find_items(articles=shown[:100], limit=200)
        except Exception as exc:                        # noqa: BLE001
            logger.warning("Поиск по всей номенклатуре не удался", exc_info=True)
            self.search_error = f"{type(exc).__name__}: {exc}"[:200]
            return []

        # ОШИБКА В ОТВЕТЕ — ТОЖЕ ОШИБКА. Обработчик 1С возвращает её кодом 200 в поле
        # `errors`, и без этой проверки сбой выглядел бы как «ничего не нашлось».
        if getattr(found, "errors", None):
            first = found.errors[0]
            self.search_error = str(first.get("message") or first)[:200]
            logger.warning("1С ответила ошибкой на поиск: %s", self.search_error)
            return []
        return [i for i in found.items if norm_article(i.article) in keys]

    def _actual_mark(self, tm_code: str, keys: set, shown: list[str]) -> tuple[str, str]:
        """Под какой маркой эти артикулы ЛЕЖАТ В 1С на самом деле.

        **СЛУЧАЙ С БОЯ (22.09.2026).** Прайс Most Flooring, коллекция «Классик»: восемь
        позиций без единой цены в 1С — работа очевидная, а задача не заводилась. Причина
        не в ценах: коллекция лежит под маркой A+ Floor, модель же сверяла её с Most
        Flooring, как написано на обложке прайса. Под той маркой артикулов нет вовсе,
        сверка отвечала «коллекция новая», сверять цены становилось не с чем — и задачи
        не возникало ни ценовой, ни какой-либо ещё.

        Догадка модели тут вообще не должна решать: принадлежность знает 1С. Спрашиваем
        её ТОЛЬКО когда по названной марке не совпал ни один артикул — то есть в том самом
        случае, который раньше молча объявлялся новой коллекцией. Один запрос из кода,
        ноль токенов.

        Марку подменяем, лишь когда все найденные живые позиции лежат под ОДНОЙ чужой:
        разнобой — повод показать, а не выбирать за админа.
        """
        if not keys:
            return tm_code, ""
        mine = {norm_article(i.article) for i in self._nomenclature(tm_code)
                if i.article and not i.not_exported}
        if mine & keys:
            return tm_code, ""          # хоть один совпал — марка та самая

        # Дерево папок отвечает точно, но видит только запрошенную марку — а вопрос как
        # раз о чужой. Поэтому оно первое, но не единственное.
        owner = self._owner_of(self._collection_name, tm_code)
        if owner and owner != tm_code:
            return owner, (f"коллекция лежит в 1С под маркой «{self._tm_name(owner)}» "
                           f"({owner}), а не под названной — сверка и задача адресованы ей")

        live = [i for i in self._search_1c(shown, keys)
                if not i.not_exported and i.tm_code]

        # ОДНОГО АРТИКУЛА МАЛО. Короткие номера повторяются у разных марок: «301»–«308»
        # оказались и у «Классик» (A+ Floor, ламинат), и у «Knit» (виниловые покрытия).
        # Двусмысленность снимается тем, что мы и так знаем: именем коллекции и видом
        # товара марки, которую назвала модель.
        narrowed = [i for i in live
                    if normalize(self._collection_name) in _collection_keys(i.parent_name)]
        if not narrowed:
            kinds = {i.product_type_ref for i in self._nomenclature(tm_code)
                     if getattr(i, "product_type_ref", "")}
            narrowed = [i for i in live
                        if getattr(i, "product_type_ref", "") in kinds] if kinds else []
        if narrowed:
            live = narrowed

        others = {i.tm_code for i in live}
        if len(others) != 1:
            return tm_code, ""
        other = others.pop()
        if other == tm_code:
            return tm_code, ""
        name = next((i.tm for i in live if i.tm_code == other), "") or other
        logger.info("Коллекция сверена не с той маркой: %s → %s", tm_code, other)
        return other, (f"артикулы этой коллекции лежат в 1С под маркой «{name}» "
                       f"({other}), а не под названной — сверка и задача адресованы ей")

    def _revival_note(self, missing: list[str], collection: str) -> list[str]:
        """Проверить, не лежат ли «недостающие» артикулы в 1С где-то ещё (§19.11).

        **ЭТО ВОЗВРАТ, А НЕ ЗАВЕДЕНИЕ.** Коллекцию, которую однажды унесли в снятые,
        поставщик может привезти снова — свой или чужой. Выгрузка марки её уже не видит
        (снятые лежат в другой ветке), и без проверки агент завёл бы всё заново: сто
        позиций-дублей и потерянная история цен.

        Спрашиваем ПАЧКОЙ, один запрос на коллекцию: цену определяет число вызовов.
        Запрос идёт из кода, то есть круга ручного цикла не стоит вовсе.
        """
        if self._onec is None or not missing:
            return []
        articles = [m.split()[0] for m in missing if m.split()]
        keys = {norm_article(a) for a in articles}

        out, refs = [], []
        for item in self._search_1c(articles, keys):
            where = item.parent_name or item.tm or "вне марки"
            out.append(f"{item.article} «{item.name}» → код {item.ref}, лежит в «{where}»")
            refs.append(item)
        if refs:
            self._revivals[normalize(collection)] = refs
        return out[:40]

    def _price_diff(self, found: list, inp: dict):
        """Сверить цены найденных позиций с прайсом (§6.1, `src/model/price_check.py`).

        Колонки называет модель — сама она их и так читает, а код по листу гадать не
        должен: у одного поставщика «дилерская», у другого «закуп», у третьего цена лежит
        третьей колонкой без заголовка вовсе. Названо один раз на лист и запоминается:
        повторять на каждой коллекции — лишние выходные токены на ровном месте.
        """
        # ЦЕНА НА ВСЮ КОЛЛЕКЦИЮ — отдельный, более частый случай. Он проще колоночного
        # и не зависит от того, как поставщик слепил артикул с названием в ячейке.
        flat = inp.get("prices") or {}
        if flat.get("purchase") is not None or flat.get("rrc") is not None:
            from_flat = price_check.flat_prices(found, flat.get("purchase"),
                                                flat.get("rrc"))
            if from_flat:
                self._remember_prices(from_flat)
                return price_check.report(price_check.compare(found, from_flat))

        spec = dict(inp.get("price_columns") or {})
        sheet_name = str(spec.pop("sheet", "") or "").strip() or self._last_sheet
        if spec:
            self._price_cols[sheet_name] = spec
        if not found:
            return "в 1С не нашлось ни одной позиции этой коллекции — сверять нечего"

        # ПУСТАЯ ЦЕНА В 1С ВИДНА И БЕЗ ПРАЙСА. У коллекции «Классик» (A+ Floor) не было
        # ни закупки, ни РРЦ ни у одной из восьми позиций — работа очевидная, а ответ
        # «колонки цен не названы» звучал как «сверить нечем», и задача не заводилась.
        # Чтобы это увидеть, прайс не нужен вовсе: достаточно посмотреть в 1С.
        nameless = sum(1 for i in found if not (i.purchase and i.purchase.value))

        spec = self._price_cols.get(sheet_name)
        if not spec:
            out = {"колонки_не_названы": "назови price_columns и позови сверку снова"}
            if nameless:
                out["без_цены_в_1С"] = nameless
                out["вывод"] = "цены нет вовсе — задачу заводи, прайс для этого не нужен"
            return out

        sheet = next((s for s in self.sheets if s.name == sheet_name), None) \
            or (self.sheets[0] if self.sheets else None)
        if sheet is None:
            return "файл не разобрался — цены сверить не с чем"

        cols = price_check.resolve_columns(sheet.rows, spec)
        if isinstance(cols, str):
            self._price_cols.pop(sheet_name, None)   # не запоминаем то, что не разрешилось
            return cols
        from_price = price_check.prices_from_rows(sheet.rows, cols)
        if not from_price:
            return ("в названных колонках цен не нашлось ни одного числа — проверь, те ли "
                    "это колонки")
        self._remember_prices(from_price)
        return price_check.report(price_check.compare(found, from_price))

    def _remember_prices(self, from_price: dict) -> None:
        """Отложить цены прайса для журнала предложений (§6.4).

        Сюда попадает то, что УЖЕ разобрано ради сверки, — ни одного лишнего действия.
        Журнал хранит цены приведёнными к базовой ЕИ, как и сравнение.
        """
        for key, prices in (from_price or {}).items():
            if not key:
                continue
            self.seen_prices[key] = {
                "purchase": float(prices["purchase"]) if "purchase" in prices else None,
                "rrc": float(prices["rrc"]) if "rrc" in prices else None,
            }

    def _nomenclature(self, tm_code: str) -> list:
        """Выгрузка марки с кешем на прогон: за составление задач она не меняется."""
        if tm_code not in self._items_cache:
            nom = self._onec.by_tm_all(tm_code, include_not_exported=True)
            self._items_cache[tm_code] = list(nom.items)
            # `.strip()` не косметика: на бою марка записана как «Most Flooring » — с
            # висячим пробелом, и он уехал бы в каждое собранное наименование.
            self._tm_names[tm_code] = (nom.tm or "").strip()
        return self._items_cache[tm_code]

    def _add(self, inp: dict) -> str:
        if len(self.collected) >= MAX_TASKS:
            return self._refuse(f"Достигнут потолок в {MAX_TASKS} задач — заканчивай.")

        raw = (inp.get("kind") or "").strip().lower()
        kind = next((k for k in TaskKind if k.value == raw), None)
        if kind is None:
            return self._refuse("Неизвестный вид задачи. Допустимо: "
                                + ", ".join(k.value for k in TaskKind))

        tm = (inp.get("tm") or "").strip()
        if not tm:
            return self._refuse("Не указана марка — без неё задача не адресуема.")

        item = (inp.get("item") or "").strip()
        collection = (inp.get("collection") or "").strip()
        mark = Ref.make(code=inp.get("tm_code"), names=[tm])

        # Нормализация адресуется МАРКОЙ, поэтому предмет ей не нужен — требовать его
        # значило бы отклонять правильно составленную задачу.
        if kind != TaskKind.NORMALIZE_NAMES and not item and not collection:
            return self._refuse(
                "Нужна коллекция либо товар: задача без предмета не адресуема.")

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

            # ПУСТУЮ ЗАДАЧУ НЕ ЗАВОДИМ. Нормализация адресована марке целиком и никакой
            # проверки до сих пор не проходила — в отличие от остальных видов, где есть
            # `compare_with_1c`. На Most Flooring вышло восемь коллекций, сто позиций и
            # НИ ОДНОЙ правки: имена давно приведены. Админ открыл задачу, выполнил и
            # увидел «менять было нечего». Такие задачи обесценивают список.
            # НОРМАЛИЗАЦИЯ ЧУЖОЙ МАРКИ — НЕ РАБОТА ЭТОГО ПРАЙСА. Тот же признак, что и у
            # снятия: доля закрытых коллекций. Агент принял заводскую пометку «Завод AGT»
            # за бренд и заказал нормализацию ВСЕГО каталога AGT — марки, о которой прайс
            # Вестерхофа говорит двумя коллекциями из семи (бой 24.09.2026).
            covered, whole = self.mark_coverage(mark.code)
            if whole and covered * 2 < whole:
                return self._refuse(
                    f"Прайс закрыл {covered} из {whole} коллекций марки «{tm}» — это не "
                    f"его марка, нормализацию по ней не завожу. Если её наименования и "
                    f"правда надо привести к шаблону, это отдельная работа по её "
                    f"собственному прайсу.")

            refusal = self._normalization_pointless(mark.code)
            if refusal:
                return self._refuse(refusal)

            # Стандарт формулирует КОД (см. `normalize_brief`). Текст агента остаётся
            # НИЖЕ и подписан как наблюдение по прайсу: в прайсе он правда кое-что видит
            # — два написания бренда, пометки NEW/АКЦИЯ в заголовках, — и терять это не
            # надо. Но стандартом 1С это не является, и выдавать одно за другое нельзя.
            seen = _price_notes_only(inp.get("description"))
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

        # ВОЗВРАТ ИЗ СНЯТЫХ дописывает КОД, а не модель. Коды 1С — то, по чему задача
        # будет исполняться, и пересказ их моделью однажды разойдётся с ответом 1С
        # (проверено на шаблоне наименований: она его выдумывала дважды). Речь о задаче
        # «добавление новых», потому что именно там дубль и возникает.
        if kind == TaskKind.ADD_NEW:
            back = self._revivals.get(normalize(collection))
            if back:
                description += "\n\n" + _revival_brief(back)

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

    def _refuse(self, reason: str) -> str:
        """Отказ заводить задачу — В ЖУРНАЛ, а не только в ответ модели.

        21.09.2026 прогон кончился нулём задач, и по журналу нельзя было сказать, почему:
        отказы жили только в истории диалога, которая никуда не сохраняется. Гадать по
        числу вызовов — не диагностика.
        """
        logger.info("add_task отклонён: %s", reason)
        return reason


PROMPT = """Ты — контент-менеджер интернет-магазина напольных покрытий. Тебе дали прайс
поставщика. Твоя работа сейчас — СОСТАВИТЬ СПИСОК ЗАДАЧ по нему, а не выполнять их.

Порядок:

1. `read_price` — посмотри, что в файле: какие листы, какие бренды и коллекции. Длинный
   лист дочитывай через `from_row`, пока не увидишь все разделы.
2. `get_selling_tm` — сопоставь бренды прайса с марками 1С. Названия бывают двуязычными
   («Latin / Кириллица») — сравнивай без учёта регистра и пунктуации.
3. `compare_with_1c` НА КАЖДУЮ КОЛЛЕКЦИЮ — передай артикулы, которые увидел в ней.
   Вернётся, чего нет в 1С, чего нет в прайсе, какие свойства пустые и КАК КОЛЛЕКЦИЯ
   НАЗЫВАЕТСЯ В 1С.
4. `add_task` на каждую пару (марка, коллекция), по которой есть что делать.

ИМЕНА КОЛЛЕКЦИЙ В ПРАЙСЕ И В 1С РАЗНЫЕ — это норма, а не ошибка. У Most Flooring в
справочнике «Millenium Pro», «Provence», «High Glossy», а в прайсе «Миллениум Про»,
«Прованс», «Супер Глянец». Сопоставляет их КОД, по артикулам, и возвращает тебе имя из
1С — адресуй задачу им.

НЕ РЕШАЙ ПО ИМЕНИ, ЧТО КОЛЛЕКЦИИ НЕТ. Пока `compare_with_1c` не сказал «ни один артикул
не нашёлся», коллекция в 1С ЕСТЬ. Задача «завести всю коллекцию» по коллекции, которая
давно заведена, создаёт дубли — на Most Flooring так едва не вышло 56 штук.

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

УЖЕ СНЯТОЕ НЕ ОБСУЖДАЙ. Коллекции с флагом «Не выгружать» сняты с производства, и сверка
их не показывает вовсе. Задач по ним не заводи и в ответе о них не упоминай: их судьба
решена, переносом в папки снятых займутся отдельно.

ЧЕГО У ТЕБЯ НЕТ ДАЖЕ СО СВЕРКОЙ: самих наименований 1С. `compare_with_1c` отвечает на
четыре вопроса — чего нет в 1С, чего нет в прайсе, какие свойства пусты и какие цены
разошлись, — и только на них. Цен 1С ты не видишь и тут: их сравнил код, тебе досталась
разница.

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
  заполнить: класс, фаска, размер, основа, упаковка. Назови и свойства, и значения.
  Отдельно смотри `без_свойства_Коллекция`: больше нуля — заводи задачу и пиши в ней,
  скольким позициям не проставлено свойство «Коллекция» и какое значение нужно. По этому
  свойству коллекция опознаётся на сайте, и пустое оно заставляет выводить имя из папки;
— «перенос в снятые» — сверка нашла позиции 1С, которых в прайсе не встретилось. Назови
  сколько и какие, и напиши, что это кандидаты: прайс мог быть частичным. **Про то, что
  попало в `есть_у_другого_поставщика`, задачу НЕ заводи** — эти позиции возит кто-то ещё,
  они не сняты с производства, у них сменился канал. Упомяни их одной строкой в ответе;
— «добавление новых» — сверка нашла артикулы прайса, которых нет в 1С. Перечисли их.
  **ПРИШЛО `коллекция_есть_в_1С` — КОЛЛЕКЦИЮ НЕ ЗАВОДИ.** Она в справочнике есть, просто
  у её позиций пуст артикул, и сверка по артикулу их не видит. Завести заново значит
  получить дубли. Вместо этого заведи «изменение свойств» на ЭТУ коллекцию: проставить
  артикулы. Строки прайса с артикулами код уже приложил — перенеси их в описание, по ним
  задачу и выполнят.
  Если в ответе есть `уже_есть_в_1С_вне_коллекции` — это ВОЗВРАТ ранее снятого, а не
  заведение: позиции в 1С есть, их надо вернуть из снятых. Коды 1С и список допишет код,
  тебе перечислять их не нужно — просто заведи задачу на эту коллекцию;
— «изменение цен» — ТОЛЬКО если сверка показала расхождение. Передай в `compare_with_1c`
  `price_columns` (где артикул, где закупка, где РРЦ — один раз на лист), и в ответе
  придёт `цены`. Пусто в `расходятся` — задачи НЕТ: цены в 1С уже такие, прогон по ней
  кончился бы словами «менять нечего». Есть расхождения — перечисли их в описании
  артикулами и числами, они уже посчитаны за тебя.
  Увидел `колонки_не_названы` — назови колонки и позови сверку ещё раз: «нечем сравнить»
  это не «совпадает». А если рядом стоит `без_цены_в_1С` — задачу заводи сразу, цены там
  нет вовсе, и прайс для этого вывода не нужен.
  ЕСЛИ ЦЕНА В ПРАЙСЕ ОДНА НА КОЛЛЕКЦИЮ (цифры стоят в строке коллекции, а у декоров ниже
  ячейки пустые) — передавай `prices`, а не колонки: по колонкам сверятся только те
  позиции, у которых цена написана в их собственной строке.

МАРКУ В АДРЕСЕ БЕРИ ИЗ СВЕРКИ. Пришло `марка_в_1С` — ставь в `add_task` ИМЕННО её `tm_code`:
коллекция лежит в 1С под другой маркой, чем написано на обложке прайса, и это нормально
(у Most Flooring часть коллекций заведена под A+ Floor).

ЗАВОД — НЕ МАРКА. В прайсе встречаются разделы «Завод AGT Турция», «Завод Peli», а в
наименованиях — приписки вроде «Альфа PELI». Это МЕСТО ПРОИЗВОДСТВА, свойство коллекции.
В справочнике 1С марки с такими именами бывают, и это СОВСЕМ ДРУГИЕ бренды: сверять с ними
коллекции этого поставщика нельзя. Бренд ищи на обложке и в названиях товаров, а не в
пометке о заводе.

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


def _add_discontinued_candidates(tools) -> list[str]:
    """Завести задачи по коллекциям 1С, которых прайс не закрыл.

    **КАНДИДАТЫ, А НЕ ПРИГОВОР.** Поставщик дробит прайс по типам товара и присылает
    частями — коллекция, которой нет в ЭТОМ файле, может лежать в соседнем. Поэтому
    задача формулируется как проверка, а решает админ.

    Заводит КОД, потому что назвать такую коллекцию агент не в состоянии: он идёт от
    прайса, а её там нет.

    **Коллекция, которую возит ДРУГОЙ поставщик, не снимается.** Это не производство
    закрылось, а сменился канал: артикулы стоят в чьём-то свежем прайсе (журнал встреч,
    `storage/sightings.py`). Задачи тогда нет вовсе — вместо неё строка в сводке. Пока
    журнал пуст, поведение прежнее: снимаем, потому что обратного никто не показывал.

    Возвращает строки для сводки — то, о чём админу сказать надо, а задачу заводить не за
    что.
    """
    notes: list[str] = []
    for tm_code, names in tools.untouched_collections().items():
        mark_name = tools._tm_names.get(tm_code, "")

        # ПРАЙС ОДНОЙ МАРКИ НИЧЕГО НЕ ГОВОРИТ О ЧУЖОЙ. Выгрузка марки попадает в кеш от
        # любого вызова сверки: агент принял заводскую пометку прайса («Завод AGT»,
        # «Peli») за бренд, а код сам переставил марку, найдя артикулы коллекции Effect
        # под AGT. После этого ВЕСЬ каталог AGT, Peli и Kronparket поехал в снятие — 18
        # задач по коллекциям, которых в прайсе Вестерхофа и быть не могло (бой
        # 24.09.2026).
        #
        # Признак «прайс про эту марку» — ДОЛЯ закрытых коллекций. Закрыл меньше половины
        # — значит прайс о ней не про неё, и судить об остальных нечем.
        covered, live = tools.mark_coverage(tm_code)
        if live and covered * 2 < live:
            notes.append(f"«{mark_name or tm_code}»: прайс закрыл {covered} из {live} "
                         f"коллекций — это не его марка, снятие по ней не предлагаю")
            continue
        mark = Ref.make(code=tm_code, names=[mark_name] if mark_name else [])
        if mark.empty:
            continue

        # Артикулы каждой коллекции — чтобы спросить журнал, не возит ли её кто-то ещё.
        articles: dict[str, list[str]] = {}
        for item in tools._items_cache.get(tm_code, ()):
            if item.article:
                articles.setdefault(collection_of(item), []).append(
                    norm_article(item.article))

        # Код папки коллекции запоминаем СРАЗУ: сейчас позиции ещё живы и ссылаются на
        # неё, а к моменту выполнения могут уехать поодиночке, и папку придётся угадывать
        # по имени. Имена папок разнородны — «Коллекция Brilliant - 10 декоров» рядом с
        # «Provence», — так что угадывание однажды промахнётся.
        folders = {}
        for item in tools._items_cache.get(tm_code, ()):
            if item.collection_ref:
                folders.setdefault(collection_of(item), item.collection_ref)

        for name in names:
            met = next((tools._elsewhere[k] for k in articles.get(name, ())
                        if k in tools._elsewhere), None)
            if met is not None:
                notes.append(f"«{name}» ({mark_name}) не в этом прайсе, но {met.label()} "
                             f"— оставлена, задачу на снятие не заводил")
                continue

            task = PriceTask(
                kind=TaskKind.MOVE_DISCONTINUED,
                address=TaskAddress(tm=mark,
                                    subject=Ref.make(code=folders.get(name),
                                                     names=[name]),
                                    subject_kind=TaskSubject.COLLECTION),
                description=(
                    f"Коллекция «{name}» есть в 1С, но в этом прайсе не встретилась "
                    f"ни одним артикулом. Проверить, снята ли она с производства, и если "
                    f"да — перенести в снятые. Поставщик мог прислать частичный прайс: "
                    f"тогда задачу просто закрыть."))
            if not any(t.matches(task) for t in tools.collected):
                tools.collected.append(task)
    return notes


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
    known = [(normalize(m["name"]), m) for m in marks if m.get("name")]

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

        mark = _mark_of_folder(normalize(node.name), known)
        if mark is None:
            continue

        for key in _collection_keys(folder.name):
            owners.setdefault(key, set()).add((mark["code"], mark["name"]))

    return owners


# Хвост имени папки вида «600x238x12»: цифры через x, ×, х (латинская и кириллическая).
_SIZE_TAIL = re.compile(r"\s+\d+(?:[x×х]\d+)+$", re.IGNORECASE)


def _collection_keys(name: str) -> set[str]:
    """Под какими именами искать эту папку. Их два, и оба нужны.

    У восьми напольных категорий размер пишется в имя ПАПКИ (§19.5): «Классик 600x238x12».
    Коллекция же зовётся «Классик» — и в прайсе, и в свойстве «Коллекция», и в адресе
    задачи. По полному имени папки такая коллекция не находилась никогда, из-за чего
    сверка марок (`fix_marks`) молча не срабатывала на всей марке A+ Floor.

    Хвост снимается только СТРОГО ПОХОЖИЙ на размер: цифры, разделённые «x». «Формат 3D»
    под это не попадает — там нет второго числа.
    """
    full = normalize(name)
    return {full, normalize(_SIZE_TAIL.sub("", " ".join(str(name or "").split())))} - {""}


def _mark_of_folder(folder_name: str, known: list) -> dict | None:
    """Марка по имени её папки. Точное совпадение либо ОКОНЧАНИЕ.

    Папка марки называется «Ламинат A+ Floor» — с видом товара впереди, — а марка в
    справочнике зовётся «A+ Floor». Точное сравнение имён не совпадало ни разу, и карта
    владельцев выходила пустой. Из нескольких подходящих берём самую длинную: «Floor» не
    должен выигрывать у «A+ Floor».
    """
    best = None
    for name, mark in known:
        if not name:
            continue
        if folder_name == name or folder_name.endswith(" " + name):
            if best is None or len(name) > len(best[0]):
                best = (name, mark)
    return best[1] if best else None


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
                usage_labels: dict | None = None,
                elsewhere: dict | None = None,
                remember=None) -> tuple[list[PriceTask], str]:
    """Прогон формирования задач. Возвращает (задачи, короткий ответ агента).

    Пустой список — не ошибка: агент мог не найти, за что зацепиться. Вызывающий решает,
    падать ли обратно на заглушку.

    `elsewhere` — снимок журнала встреч по ЧУЖИМ поставщикам (артикул → где видели);
    `remember(артикулы)` — куда сложить то, что показал этот прайс. Оба необязательны:
    без них поведение прежнее, то есть «нет в прайсе — кандидат в снятые».
    """
    tools = TaskBuilderTools(content, filename, onec=onec, elsewhere=elsewhere)
    task = f"Прайс «{filename}». Составь список задач по нему."

    answer, _ = await orchestrator.handle_turn(
        [{"role": "user", "content": task}], system=PROMPT, extra_tools=TOOLS,
        extra_executor=tools, base_tools=False, usage_labels=usage_labels)

    # КОЛЛЕКЦИИ, КОТОРЫХ ПРАЙС НЕ КОСНУЛСЯ, — задача заводится КОДОМ.
    #
    # Агент их назвать не может: он идёт от прайса, а такая коллекция есть только в 1С.
    # Именно так осталась незамеченной Brilliant у Most Flooring — восемь живых позиций.
    if onec is not None and tools.collected:
        try:
            kept = _add_discontinued_candidates(tools)
            if kept:
                # Это не задача, а факт: коллекция жива, её возит другой. Молчать нельзя —
                # админ помнит, что в прошлый раз её предлагали снять.
                answer += "\n\nНе сняты, потому что есть у других поставщиков:\n— " \
                          + "\n— ".join(kept)
        except Exception:                               # noqa: BLE001
            logger.warning("Не удалось найти коллекции вне прайса", exc_info=True)

    # ЖУРНАЛ ВСТРЕЧ ПОПОЛНЯЕТСЯ ВСЕГДА — даже когда задач не вышло ни одной.
    #
    # Ценность журнала в полноте: прайс, по которому делать было нечего, всё равно
    # доказывает, что коллекция жива. Пишем ПОСЛЕ хода, потому что артикулы приносит сам
    # ход — модель перечисляет их в `compare_with_1c`.
    if remember is not None and tools.seen_articles:
        try:
            await remember(tools.seen_articles, tools.seen_prices)
        except Exception:                               # noqa: BLE001
            logger.warning("Не удалось записать журнал встреч", exc_info=True)

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
