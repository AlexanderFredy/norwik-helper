"""Инструменты режима «обновление цен» (админский сценарий, specs/content-manager.md).

Все инструменты здесь — ЧТЕНИЕ 1С и подготовка предложения. Записи цен среди них нет
намеренно: payload сохраняется в `pending_proposal`, а отправляет его в 1С только
обработчик кнопки подтверждения (§10 спеки). Модель физически не может записать цены.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from datetime import date
from decimal import Decimal, InvalidOperation

from src.onec.client import NomItem, Nomenclature, OnecClient
from src.price_tool.changes import (
    GroupResult, build_payload, plan_collection, plan_items, unit_warnings,
)
from src.price_tool.exclusive import (
    HINT_WORDS, WHERE_FOUND, annotate, find, resolve,
)
from src.price_tool.parser import (
    extract_images, find_rows, non_empty_rows, parse_price_table, render_preview,
)
from src.price_tool.scope import describe, in_scope
from src.price_tool.signature import price_signature
from src.storage.price_files import save as save_price_file
from src.storage.pricing import PricingStore

logger = logging.getLogger(__name__)

# Картинки едут в истории диалога (SQLite + каждый следующий запрос к модели), поэтому
# лимиты жёсткие: логотипы весят десятки килобайт, всё крупное — это фото товаров.
MAX_TEXT_CHARS = 40000
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 1_500_000
MAX_IMAGE_TOTAL_BYTES = 4_000_000

# Номенклатура ТМ живёт дольше одного хода диалога: PricingTools создаётся заново на
# каждое сообщение админа, и без общего кэша каждый его ответ («плинтус не трогай»)
# заново выкачивал бы из 1С все ТМ прайса — минуты молчания на мультибрендовом прайсе.
# Кэш сбрасывается при новом прайсе и после записи цен (см. clear_nomenclature_cache).
# Марка крупнее этого разбирается по коллекциям (§9.6.2): держать в одном ходу и всю
# номенклатуру, и весь раздел прайса модель не может — на Atlas Concorde Rus (932 поз.)
# она упёрлась и переложила решение на админа.
BIG_TM_ITEMS = 300

# С какого размера прайс считается дорогим и требует решения админа (§6.10). Замер: прайс
# Артисана на 12 870 строк обошёлся примерно в 570 тыс. входных токенов за прогон — по нему
# и построена оценка ниже. Порог намеренно высокий: обычный прайс поставщика в него не
# попадает, вопрос должен быть редким.
BIG_PRICE_ROWS = 5000
MEASURED_ROWS, MEASURED_TOKENS = 12_870, 570_000
NOM_CACHE_TTL = 1800
_NOM_CACHE: dict[str, tuple[float, list]] = {}
# код ТМ → имя из selling-tm. В NomItem названия марки нет, а брать первое слово из
# наименования товара нельзя: «Ламинат Woodstyle Opera …» даёт «Ламинат», а не бренд.
_TM_NAMES: dict[str, str] = {}


def clear_nomenclature_cache() -> None:
    _NOM_CACHE.clear()
    _TM_NAMES.clear()

def next_step(run: dict | None) -> str | None:
    """Что обрабатывать следующим: коллекция текущей марки либо следующая марка."""
    if not run:
        return None
    stage = run.get("stage")
    if stage and stage.get("remaining"):
        return stage["remaining"][0]["name"]
    return run["remaining"][0]["name"] if run["remaining"] else None


def queue_tail(run: dict | None, skip_tm: str | None = None,
               skip_coll: str | None = None) -> str:
    """«Сейчас марка X, осталось в ней …» + «Осталось обработать …» по маркам.

    Живёт на уровне модуля, а не в классе: тот же хвост дописывает обработчик кнопок
    после записи, пропуска и откладывания.
    """
    if not run:
        return ""
    lines = []
    stage = run.get("stage")
    if stage:
        left = [c["name"] for c in stage.get("remaining") or [] if c["ref"] != skip_coll]
        head = f"Сейчас {stage.get('tm_name') or stage.get('tm_code')}"
        lines.append(f"{head}. Осталось в марке: {', '.join(left)}." if left
                     else f"{head} — последняя коллекция.")
    # текущая марка из внешней очереди не выпадает, пока её коллекции не кончились
    tms = [t["name"] for t in run["remaining"]
           if t["code"] != (stage.get("tm_code") if stage else skip_tm)]
    if tms:
        lines.append(f"Осталось обработать: {', '.join(tms)}.")
    return ("\n\n" + "\n".join(lines)) if lines else ""


PRICING_TOOLS = [
    {
        "name": "read_price_file",
        "description": (
            "Читает присланный админом файл прайса (xlsx/xls/csv/pdf) и возвращает его "
            "таблицей. Параметр sheet — подстрока имени листа (для многолистовых прайсов; "
            "без него вернутся все листы). Вызывай в начале работы с прайсом.\n"
            "Если в ответе сказано, что показаны НЕ ВСЕ строки, — обязательно дочитай "
            "остаток: вызови ещё раз с тем же sheet и from_row из подсказки. Не сообщай "
            "админу, что хвост прайса тебе не виден: он виден, его надо запросить.\n"
            "БОЛЬШОЙ ПРАЙС (тысячи строк) НЕ ЛИСТАЙ ПОДРЯД: каждая выгрузка оседает в "
            "переписке и раздувает её. Вместо этого найди нужный раздел параметром "
            "contains (например contains=«Ceracasa») — вернутся НОМЕРА строк с этим "
            "текстом, — и прочитай только его: from_row = номер начала раздела."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sheet": {"type": "string"},
                "from_row": {"type": "integer",
                             "description": "с какой непустой строки листа продолжить (с 1)"},
                "contains": {"type": "string",
                             "description": "искать строки с этим текстом и вернуть их НОМЕРА (название бренда, коллекции). Дёшево — так ищут начало раздела в большом прайсе"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "save_price_mapping",
        "description": (
            "Запомнить трактовку колонок ОДНОГО ЛИСТА прайса, чтобы в следующий раз не "
            "спрашивать админа заново. Вызывай СРАЗУ ПОСЛЕ того, как админ ответил на "
            "вопрос о колонках (какая закупка, какая РРЦ, цена за м² или за упаковку). "
            "Привязка идёт к структуре файла, а не к имени файла: следующий прайс того же "
            "поставщика в том же формате подхватит её автоматически.\n"
            "У мультилистового прайса вызывай по разу НА КАЖДЫЙ лист с ценами, указывая "
            "sheet: трактовки листов хранятся отдельно и друг друга не затирают. "
            "Сохранение одного листа НЕ означает, что остальные обрабатывать не надо."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier": {"type": "string", "description": "поставщик — для показа админу"},
                "purchase_column": {"type": "string", "description": "заголовок колонки закупки, дословно"},
                "rrc_column": {"type": "string", "description": "заголовок колонки РРЦ; опустить, если её нет"},
                "basis": {"type": "string", "description": "base_unit (цена за базовую ЕИ) или package (за упаковку)"},
                "sheet": {"type": "string", "description": "ИМЯ ЛИСТА, к которому относится трактовка. Обязательно, если листов несколько: у каждого листа своя запись, вызывай инструмент по разу на лист"},
                "note": {"type": "string", "description": "чем именно был обусловлен выбор (слова админа)"},
            },
            "required": ["purchase_column"],
            "additionalProperties": False,
        },
    },
    {
        "name": "start_price_run",
        "description": (
            "Объявить план работы: какие торговые марки прайса ты будешь обрабатывать. "
            "Вызывай ОДИН раз, после get_selling_tm и определения брендов, до первого "
            "сопоставления. Перечисли только те ТМ, что есть в выгрузке и подходят по "
            "категориям товаров. Дальше марки обрабатываются ПО ОДНОЙ: сопоставил → "
            "propose_prices по этой марке → админ нажал кнопку → переходишь к следующей."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier": {"type": "string"},
                "price_doc": {"type": "string", "description": "как называть прайс в отчётах"},
                "price_date": {"type": "string", "description": "дата прайса ГГГГ-ММ-ДД из шапки или имени файла — по ней видно, не устарели ли отложенные задачи"},
                "trademarks": {
                    "type": "array",
                    "description": "в порядке обработки",
                    "items": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "description": "код ТМ из get_selling_tm"},
                            "name": {"type": "string"},
                            "first_row": {"type": "integer", "description": "с какой строки прайса начинается раздел бренда — запомню, чтобы при повторной присылке того же файла не искать заново"},
                            "last_row": {"type": "integer"},
                        },
                        "required": ["code", "name"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["trademarks"],
            "additionalProperties": False,
        },
    },
    {
        "name": "start_tm_collections",
        "description": (
            "Разбить крупную марку на коллекции и обрабатывать их по очереди. Вызывай, "
            "когда propose_prices отказал из-за объёма марки.\n"
            "Перечисли ТОЛЬКО те коллекции 1С, которые реально встречаются в прайсе, — "
            "иначе очередь забьётся коллекциями, по которым нечего менять. Дальше "
            "передавай propose_prices по ОДНОЙ коллекции, как марки в общем плане."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tm_code": {"type": "string"},
                "tm_name": {"type": "string"},
                "collections": {
                    "type": "array",
                    "description": "в порядке обработки",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ref": {"type": "string", "description": "collection_ref из get_1c_nomenclature"},
                            "name": {"type": "string"},
                        },
                        "required": ["ref"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["tm_code", "collections"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_price_decision",
        "description": (
            "Записать ответ админа по КРУПНОМУ прайсу: разбирать его или админ обновит "
            "цены вручную. Вызывай только после явного ответа админа на вопрос, который "
            "ты задал по подсказке из read_price_file.\n"
            "decision=process — разбираем обычным порядком, вопрос больше не повторится.\n"
            "decision=manual — не разбираем: прайс считается обработанным, в отложенные "
            "НЕ попадает, админ обновит цены сам."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "description": "process | manual"},
                "supplier": {"type": "string"},
                "price_doc": {"type": "string"},
                "price_date": {"type": "string", "description": "дата прайса ГГГГ-ММ-ДД"},
                "reason": {"type": "string", "description": "словами админа, если назвал"},
            },
            "required": ["decision"],
            "additionalProperties": False,
        },
    },
    {
        "name": "defer_task",
        "description": (
            "Отложить марку или коллекцию: админ сказал «пропустим, вернёмся позже». "
            "Задача попадёт в список отложенных, переживёт конец прогона и перезапуск, "
            "а прайс сохранится на сервере, чтобы вернуться без пересылки файла.\n"
            "Вызывай ТОЛЬКО по явной просьбе админа: у него есть кнопка «Отложить» под "
            "предложением, и сам по себе пропуск задачу не заводит."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tm_code": {"type": "string"},
                "tm_name": {"type": "string"},
                "collection_ref": {"type": "string", "description": "если откладываем одну коллекцию"},
                "collection": {"type": "string"},
                "reason": {"type": "string", "description": "словами админа, если назвал"},
            },
            "required": ["tm_code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_final_note",
        "description": (
            "Отложить замечание, которое относится к ПРАЙСУ ЦЕЛИКОМ, а не к текущей марке. "
            "Оно будет показано админу ОДИН раз, в самом конце, перед «прайс обработан "
            "полностью». Повторы отсеиваются автоматически.\n"
            "Сюда: бренды прайса не из выгрузки на сайт; листы других категорий товаров; "
            "листы, которые не разбирались, и почему; общие замечания по файлу.\n"
            "НЕ сюда (это в warnings текущего предложения): несопоставленные коллекции "
            "разбираемой марки, её коллекции из 1С без строк в прайсе, вопросы и "
            "расхождения по ней. Правило простое: относится к одной марке — в предложение, "
            "ко всему файлу — сюда."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "notes": {"type": "array", "items": {"type": "string"},
                          "description": "по одной законченной формулировке на элемент"},
            },
            "required": ["notes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_product_scope",
        "description": (
            "Категории товаров, которые админ разрешил анализировать (задаются один раз "
            "на все прайсы, а не на каждый файл). Разделы прайса других категорий "
            "разбирать не нужно — их достаточно перечислить одной строкой в "
            "предупреждениях. Пустой список означает, что ограничений нет. "
            "Вызывай в начале работы с прайсом, до сопоставления. Без параметров."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_selling_tm",
        "description": (
            "Торговые марки, выгружаемые на сайт: [{name, code}]. Бренд прайса, которого "
            "здесь нет, не обрабатываем — только сообщаем админу. Без параметров."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_1c_nomenclature",
        "description": (
            "Номенклатура одной ТМ с текущими ценами (закупка/розница/РРЦ) и коэффициентами "
            "ЕИ. Параметры: tm_code (код из get_selling_tm), page (с 1), size (200). "
            "У товара есть collection_ref — код папки-коллекции, он нужен для предложения.\n"
            "Работаешь по одной коллекции — ОБЯЗАТЕЛЬНО передавай collection_ref: вернутся "
            "только её товары. Выгрузка всей марки на 900 позиций весит десятки тысяч "
            "символов, едет в каждый следующий запрос и стоит дорого."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tm_code": {"type": "string"},
                "collection_ref": {"type": "string", "description": "код папки-коллекции: вернуть только её товары"},
                "page": {"type": "integer"},
                "size": {"type": "integer"},
            },
            "required": ["tm_code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "record_exclusives",
        "description": (
            "Запомнить, что поставщик заявил в прайсе ЭКСКЛЮЗИВ на коллекцию или товар "
            "(«эксклюзив», «эксклюзивный дистрибьютор», «только у нас», «exclusive»). "
            "Вызывай ПОСЛЕ сопоставления с 1С и ДО propose_prices, если такие надписи в "
            "прайсе есть. Пометка чисто справочная — на цены она не влияет.\n"
            "ВАЖНО: слово, входящее в САМО НАЗВАНИЕ товара или коллекции (коллекция "
            "«Exclusive», «Kronotex Exclusive»), заявкой НЕ является — это имя, а не "
            "заявление о правах. Засчитывай, только если надпись стоит ОТДЕЛЬНО: в "
            "колонке-примечании, в строке-заголовке раздела, в имени листа или файла."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier": {"type": "string", "description": "поставщик, чей это прайс"},
                "price_date": {"type": "string", "description": "дата прайса ГГГГ-ММ-ДД; без неё — сегодняшняя"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tm_code": {"type": "string"},
                            "tm_name": {"type": "string"},
                            "collection_ref": {"type": "string", "description": "код папки-коллекции; опустить, если эксклюзив на всю ТМ"},
                            "collection": {"type": "string"},
                            "item_ref": {"type": "string", "description": "код товара, если эксклюзив на одну позицию"},
                            "item_name": {"type": "string"},
                            "phrase": {"type": "string", "description": "надпись из прайса ДОСЛОВНО"},
                            "where_found": {"type": "string", "description": "column | header | sheet | filename | admin"},
                        },
                        "required": ["tm_code", "phrase", "where_found"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["supplier", "items"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_exclusive",
        "description": (
            "Записать решение админа по спорному эксклюзиву (когда несколько поставщиков "
            "заявили его на одно и то же). Вызывай ТОЛЬКО после явного ответа админа. "
            "supplier — кого он выбрал; чтобы снять пометку совсем, передай supplier = "
            "\"none\"."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tm_code": {"type": "string"},
                "collection_ref": {"type": "string"},
                "item_ref": {"type": "string"},
                "supplier": {"type": "string", "description": "выбранный поставщик либо \"none\""},
                "note": {"type": "string", "description": "причина словами админа, если назвал"},
            },
            "required": ["tm_code", "supplier"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_prices",
        "description": (
            "Подготовить предложение по обновлению цен и показать его админу. Передай "
            "сопоставленные коллекции с ценами ИЗ ПРАЙСА, приведёнными к базовой ЕИ товара. "
            "Розницу НЕ передавай — она считается автоматически по правилам магазина. "
            "Инструмент сам сравнит с текущими ценами 1С, отбросит изменения меньше 2%, "
            "посчитает розницу и вернёт готовый текст предложения. Ничего не записывает: "
            "запись выполнит админ кнопкой. Вызывай ОДИН раз на марку.\n"
            "ДВА СПОСОБА задать цену в группе:\n"
            "• purchase/rrc на всю коллекцию — когда все её товары стоят одинаково "
            "(ламинат, виниловый ламинат: коллекция = один декор в разных цветах);\n"
            "• items[{ref, purchase, rrc}] — цена на каждый товар отдельно. Так задаётся "
            "КЕРАМИЧЕСКАЯ ПЛИТКА и КЕРАМОГРАНИТ: в одной коллекции лежат настенная, "
            "напольная, декор, бордюр, вставка, ступень — у каждого своя цена и свой размер. "
            "Указывай items — трогаются только перечисленные товары, остальные в папке "
            "остаются как есть."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier": {"type": "string", "description": "поставщик, как его назвал админ или как в прайсе"},
                "price_doc": {"type": "string", "description": "как назвать прайс менеджерам, напр. «Монарх-логистик»; по умолчанию имя файла"},
                "price_date": {"type": "string", "description": "дата самого прайса ГГГГ-ММ-ДД, если она видна в шапке или имени файла"},
                "groups": {
                    "type": "array",
                    "description": "по одной записи на коллекцию 1С",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tm_code": {"type": "string"},
                            "tm_name": {"type": "string"},
                            "collection_ref": {"type": "string", "description": "код папки-коллекции из get_1c_nomenclature"},
                            "purchase": {"type": "number", "description": "закупка из прайса за базовую ЕИ; опустить, если в прайсе нет"},
                            "rrc": {"type": "number", "description": "РРЦ из прайса за базовую ЕИ; опустить, если в прайсе нет"},
                            "items": {
                                "type": "array",
                                "description": "поэлементные цены (плитка/керамогранит). Если задано, purchase/rrc группы игнорируются, а товары вне списка не трогаются",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "ref": {"type": "string", "description": "Код товара 1С из get_1c_nomenclature"},
                                        "purchase": {"type": "number"},
                                        "rrc": {"type": "number"},
                                    },
                                    "required": ["ref"],
                                    "additionalProperties": False,
                                },
                            },
                            "note": {"type": "string"},
                        },
                        "required": ["tm_code", "collection_ref"],
                        "additionalProperties": False,
                    },
                },
                "warnings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "что показать админу: не сопоставленные строки, бренды не в выгрузке, расхождения",
                },
            },
            "required": ["groups"],
            "additionalProperties": False,
        },
    },
]


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _fmt(v) -> str:
    if v is None:
        return "—"
    d = Decimal(str(v))
    s = f"{d:,.0f}" if d == d.to_integral_value() else f"{d:,.2f}"
    return s.replace(",", " ")


def _pct(old, new) -> str:
    if not old:
        return "впервые"
    return f"{(Decimal(str(new)) - Decimal(str(old))) / Decimal(str(old)) * 100:+.1f}%"


def _transitions(group: GroupResult, kind: str, label: str) -> list[str]:
    """«было → стало», сгруппированное по одинаковому переходу."""
    buckets: dict[tuple, int] = {}
    for p in group.plans:
        if kind in p.prices:
            key = (p.before.get(kind), p.prices[kind])
            buckets[key] = buckets.get(key, 0) + 1
    out = []
    for (old, new), n in buckets.items():
        out.append(f"{label} {_fmt(old)} → {_fmt(new)} ({_pct(old, new)}), {n} поз.")
    return out


class PricingTools:
    """Исполнитель инструментов режима цен. Живёт в рамках одного пользователя."""

    def __init__(self, onec: OnecClient, store: PricingStore, user_id: int) -> None:
        self._onec = onec
        self._store = store
        self._user_id = user_id
        self._file: tuple[str, bytes] | None = None      # (имя, содержимое) текущего прайса
        self._images: list[dict] = []                    # баннеры из прайса — для модели
        self.last_summary: str | None = None
        # марка закрылась без кнопки — обработчик сам двинет очередь (§9.6)
        self.advanced_to: str | None = None
        # админ решил обновить крупный прайс вручную — выходим из режима
        self.handled_manually = False

    def set_file(self, filename: str, content: bytes) -> None:
        self._file = (filename, content)

    def handles(self, name: str) -> bool:
        return name in {t["name"] for t in PRICING_TOOLS}

    async def execute(self, name: str, inp: dict) -> str | list[dict]:
        try:
            if name == "read_price_file":
                return await self._read_with_mapping(inp)
            if name == "save_price_mapping":
                return await self._save_mapping(inp)
            if name == "start_price_run":
                return await self._start_run(inp)
            if name == "start_tm_collections":
                return await self._start_stage(inp)
            if name == "set_price_decision":
                return await self._set_decision(inp)
            if name == "defer_task":
                return await self._defer(inp)
            if name == "add_final_note":
                return await self._add_note(inp)
            if name == "get_product_scope":
                return await self._product_scope()
            if name == "get_selling_tm":
                return await asyncio.to_thread(self._selling_tm)
            if name == "get_1c_nomenclature":
                return await asyncio.to_thread(self._nomenclature, inp)
            if name == "record_exclusives":
                return await self._record_exclusives(inp)
            if name == "set_exclusive":
                return await self._set_exclusive(inp)
            if name == "propose_prices":
                return await self._propose(inp)
            return f"Неизвестный инструмент: {name}"
        except Exception as exc:                       # noqa: BLE001
            logger.exception("Ошибка инструмента %s", name)
            return f"Ошибка выполнения {name}: {exc}"

    # ------------------------------------------------------------------ чтение

    def _file_key(self) -> str:
        """Хеш СОДЕРЖИМОГО — им опознаётся «тот же самый прайс» (§6.9, §6.10).

        Сигнатура (§6.5.2) для этого не годится: она считается по скелету файла, поэтому
        прайс того же поставщика за следующий месяц имеет ту же сигнатуру при других
        строках — решение «обрабатываю вручную» молча перешло бы на новый файл.
        """
        return hashlib.sha1(self._file[1]).hexdigest()[:16] if self._file else ""

    def _signature(self) -> str:
        """Сигнатура структуры текущего прайса — ключ маппинга (§6.5.2)."""
        if not self._file:
            return ""
        filename, content = self._file
        sheets = parse_price_table(content, filename)
        if sheets:
            return price_signature(sheets)
        from src.email_tool.attachments import extract_text
        return price_signature([], extract_text(filename, content))

    @staticmethod
    def _mapping_line(known: dict) -> str:
        m = known["mapping"]
        parts = [f"закупка = «{m.get('purchase_column')}»"]
        if m.get("rrc_column"):
            parts.append(f"РРЦ = «{m['rrc_column']}»")
        if m.get("basis"):
            parts.append(f"база цены: {m['basis']}")
        note = f" Основание: {m['note']}." if m.get("note") else ""
        where = f"лист «{known['sheet']}»" if known.get("sheet") else "весь файл"
        return f"— {where}: " + "; ".join(parts) + f".{note}"

    async def _read_with_mapping(self, inp: dict) -> str | list[dict]:
        text = await asyncio.to_thread(self._read_price_file, inp)
        scope = await self._scope_note()
        signature = await asyncio.to_thread(self._signature)
        if not signature:
            return self._attach(text + scope)
        # разбор кеширован (§6.9), поэтому получить листы здесь заново почти бесплатно
        sheets = await asyncio.to_thread(parse_price_table, self._file[1], self._file[0])
        file_key = await asyncio.to_thread(self._file_key)
        decided = await self._big_price_gate(file_key, sheets, inp)
        if decided:
            return decided
        layout = await self._layout_note(file_key)
        known = await self._store.get_mappings(signature)
        if not known:
            return self._attach(
                text + scope + layout
                + "\n\n[Формат прайса встречается впервые: трактовку колонок нужно "
                "определить, при неоднозначности — спросить админа и сохранить "
                "через save_price_mapping (по одному вызову на КАЖДЫЙ лист с ценами).]")

        supplier = next((k["supplier"] for k in known if k.get("supplier")), None)
        covered = [k["sheet"] for k in known if k.get("sheet")]
        block = ["\n\n[ЗАПОМНЕННЫЙ МАППИНГ этого формата прайса"
                 + (f" (поставщик: {supplier})" if supplier else "") + ":"]
        block += [self._mapping_line(k) for k in known]
        # Раньше маппинг хранился один на файл, и агент сужал работу до его листа: у
        # мультилистового прайса остальные листы молча выпадали (см. §6.5.1).
        block.append(
            "Применяй эти трактовки МОЛЧА, вопрос о колонках по ним не повторяй."
            + (f" Запомнены только листы: {', '.join(covered)}. " if covered else " ")
            + "ОСТАЛЬНЫЕ листы прайса это НЕ отменяет: разбери и их — определи колонки "
              "сам, а при неоднозначности спроси админа и сохрани через "
              "save_price_mapping с указанием листа. Пропускать лист можно только "
              "потому, что его категория товара вне анализа, или потому, что так сказал "
              "админ — но об этом всё равно скажи в предложении.]")
        return self._attach(text + scope + layout + "\n".join(block))

    async def _start_run(self, inp: dict) -> str:
        tms = [{"code": t["code"], "name": t.get("name") or t["code"],
                **{k: t[k] for k in ("first_row", "last_row") if t.get(k)}}
               for t in inp.get("trademarks") or [] if t.get("code")]
        if not tms:
            return "Не переданы торговые марки — план не принят."
        doc = inp.get("price_doc") or (self._file[0] if self._file else None)
        date_str = (inp.get("price_date") or "")[:10]
        await self._store.start_run(self._user_id, inp.get("supplier"), doc, tms,
                                    price_date=date_str)
        stale_note = ""
        if date_str:
            signature = await asyncio.to_thread(self._signature) if self._file else None
            stale = await self._store.mark_stale(self._user_id, inp.get("supplier"),
                                                 signature, date_str)
            if stale:
                what = "; ".join(
                    (f"{t['tm_name'] or t['tm_code']}"
                     + (f" / {t['collection']}" if t["collection"] else "")
                     + f" (прайс от {t['price_date'][:10]})") for t in stale[:5])
                stale_note = (f"\n\n⚠️ Скажи админу: этот прайс от {date_str} новее, чем "
                              f"прайсы отложенных задач — {what}. Они устарели, снять: "
                              "/deferred_clear_stale")
        # раскладку запоминаем по сигнатуре файла: тот же прайс, присланный заново,
        # разведку брендов повторять не заставит (§6.9)
        file_key = await asyncio.to_thread(self._file_key)
        if file_key:
            await self._store.drop_old_layouts(inp.get("supplier"), date_str)
            await self._store.save_layout(file_key, inp.get("supplier"), doc,
                                          date_str, tms)
        names = ", ".join(t["name"] for t in tms)
        return (f"План принят: {len(tms)} марок — {names}." + stale_note + "\n"
                f"Обрабатывай ПО ОДНОЙ, начни с «{tms[0]['name']}»: сопоставь её коллекции "
                "и вызови propose_prices только по ней. К следующей переходи после того, "
                "как админ нажмёт кнопку.")

    async def _start_stage(self, inp: dict) -> str:
        colls = [c for c in inp.get("collections") or [] if c.get("ref")]
        if not colls:
            return "Не переданы коллекции — очередь по марке не начата."
        run = await self._store.start_stage(
            self._user_id, inp["tm_code"], inp.get("tm_name") or inp["tm_code"], colls)
        left = (run or {}).get("stage", {}).get("remaining") or []
        names = ", ".join(c["name"] for c in left)
        return (f"Марка разбита на {len(left)} коллекций: {names}.\n"
                f"Начни с «{left[0]['name']}» — propose_prices по ней одной. К следующей "
                "переходи после кнопки админа, как и с марками.")

    async def _defer(self, inp: dict) -> str:
        saved = await self._store.defer_task(self._user_id, {
            **{k: inp.get(k) for k in ("tm_code", "tm_name", "collection_ref",
                                       "collection", "reason")},
            **(await self.run_context()),
        })
        what = inp.get("collection") or inp.get("tm_name") or inp.get("tm_code")
        if not saved:
            return f"«{what}» уже в списке отложенных — повторно не завожу."
        return (f"Отложено: {what}. Прайс сохранён, вернуться можно командой "
                "/deferred_resume без пересылки файла.")

    async def run_context(self) -> dict:
        """Реквизиты прайса для отложенной задачи + сам файл на диск."""
        run = await self._store.get_run(self._user_id) or {}
        ctx = {"supplier": run.get("supplier"), "price_doc": run.get("price_doc"),
               "price_date": run.get("price_date"), "signature": None, "file_path": None}
        if self._file:
            ctx["signature"] = await asyncio.to_thread(self._signature)
            path = await asyncio.to_thread(save_price_file, self._store.db_path,
                                           self._file[0], self._file[1])
            ctx["file_path"] = str(path) if path else None
        return ctx

    async def _add_note(self, inp: dict) -> str:
        added = await self._store.add_run_notes(self._user_id, inp.get("notes") or [])
        if not added:
            return ("Замечание уже записано (или прогон не начат) — повторять его в тексте "
                    "предложения не нужно.")
        return (f"Отложено до конца прайса: {added}. В предложение по текущей марке это "
                "НЕ включай — админ увидит всё разом после последней марки.")

    async def _product_scope(self) -> str:
        scope = [c["category"] for c in await self._store.list_scope()]
        return json.dumps({"categories": scope, "instruction": describe(scope)},
                          ensure_ascii=False)

    @staticmethod
    def _row_count(sheets: list) -> int:
        return sum(len(non_empty_rows(sh)) for sh in sheets)

    async def _big_price_gate(self, file_key: str, sheets: list, inp: dict) -> str | None:
        """Крупный прайс: спросить админа, разбирать ли, до того как тратиться (§6.10).

        Возвращает текст-остановку либо None, если можно работать дальше. Решение помнится
        по сигнатуре файла: повторная присылка того же прайса вопрос не повторяет.
        """
        known = await self._store.get_decision(file_key)
        if known and known["decision"] == "manual":
            doc = known.get("price_doc") or "прайс"
            return (f"[По этому прайсу («{doc}») админ уже решил, что обработает его "
                    f"ВРУЧНУЮ ({(known.get('decided_at') or '')[:10]}). Разбирать не нужно: "
                    "скажи админу об этом и остановись. В отложенные он не идёт — прайс "
                    "считается обработанным.]")
        if known and known["decision"] == "process":
            return None                      # уже согласовано, вопрос не повторяем

        rows = await asyncio.to_thread(self._row_count, sheets)
        if rows < BIG_PRICE_ROWS:
            return None

        estimate = int(rows / MEASURED_ROWS * MEASURED_TOKENS)
        n = f"{rows:,}".replace(",", " ")
        cost = f"{estimate:,}".replace(",", " ")
        return (f"[КРУПНЫЙ ПРАЙС: {n} строк на {len(sheets)} листах. По замеру на прайсе "
                f"такого же размера разбор обойдётся примерно в {cost} входных токенов — "
                "это заметные деньги. НЕ НАЧИНАЙ разбор. Спроси админа ровно одно: "
                "обработать этот прайс или пропустить (он обновит цены вручную). Назови "
                "ему число строк и оценку. Получив ответ, вызови set_price_decision с "
                "process или manual и действуй по ответу.]")

    async def _set_decision(self, inp: dict) -> str:
        file_key = await asyncio.to_thread(self._file_key)
        if not file_key:
            return "Не удалось опознать файл — решение не сохранено."
        decision = "manual" if inp.get("decision") == "manual" else "process"
        run = await self._store.get_run(self._user_id) or {}
        rows = None
        if self._file:
            sheets = await asyncio.to_thread(parse_price_table, self._file[1], self._file[0])
            rows = await asyncio.to_thread(self._row_count, sheets)
        await self._store.save_decision(
            file_key, decision, inp.get("supplier") or run.get("supplier"),
            inp.get("price_doc") or (self._file[0] if self._file else None),
            inp.get("price_date") or run.get("price_date"), rows, inp.get("reason"))
        if decision == "manual":
            self.handled_manually = True
            return ("Записано: прайс обработает админ вручную. В отложенные он НЕ идёт и "
                    "считается обработанным. Скажи админу об этом и остановись — разбирать "
                    "ничего не нужно.")
        return "Записано: прайс разбираем. Продолжай обычным порядком, вопрос повторять не буду."

    async def _layout_note(self, file_key: str) -> str:
        """Если этот же файл уже разбирался — отдаём готовую раскладку брендов."""
        layout = await self._store.get_layout(file_key)
        if not layout or not layout.get("sections"):
            return ""
        rows = []
        for sec in layout["sections"]:
            where = ""
            if sec.get("first_row"):
                where = f", строки {sec['first_row']}" + (
                    f"–{sec['last_row']}" if sec.get("last_row") else " и далее")
            rows.append(f"{sec.get('name') or sec.get('code')} ({sec.get('code')}){where}")
        return ("\n\n[ЭТОТ ПРАЙС УЖЕ РАЗБИРАЛСЯ " + (layout.get("updated_at") or "")[:10]
                + ". Разведку брендов повторять НЕ НУЖНО, вот сохранённая раскладка: "
                + "; ".join(rows) + ". Передай в start_price_run этот же список и сразу "
                "читай нужный раздел через from_row.]")

    async def _scope_note(self) -> str:
        scope = [c["category"] for c in await self._store.list_scope()]
        return "\n\n[КАТЕГОРИИ ТОВАРОВ. " + describe(scope) + "]"

    async def _save_mapping(self, inp: dict) -> str:
        signature = await asyncio.to_thread(self._signature)
        if not signature:
            return "Не удалось вычислить сигнатуру прайса — маппинг не сохранён."
        sheet = (inp.get("sheet") or "").strip()
        mapping = {k: v for k, v in inp.items() if k not in ("supplier", "sheet") and v}
        await self._store.save_mapping(signature, inp.get("supplier"), mapping, sheet)
        where = f"листа «{sheet}»" if sheet else "этого прайса"
        return (f"Маппинг {where} сохранён — трактовки других листов не затронуты. "
                "Если в прайсе есть ещё листы с ценами, сохрани и их отдельными вызовами.")

    # -------------------------------------------------------------- эксклюзивы (§9.5)

    async def _record_exclusives(self, inp: dict) -> str:
        """Заявки об эксклюзиве из прайса + сообщение о спорах, если они возникли."""
        supplier = (inp.get("supplier") or "").strip()
        if not supplier:
            return "Не указан поставщик — заявка об эксклюзиве не записана."
        price_date = (inp.get("price_date") or "")[:10] or date.today().isoformat()

        claims, in_name, bad_place = [], [], []
        for item in inp.get("items") or []:
            name = f"{item.get('collection') or ''} {item.get('item_name') or ''}".lower()
            if any(word in name for word in HINT_WORDS):
                # «Kronotex Exclusive» — это имя коллекции, а не заявление о правах;
                # принять такую заявку значит навесить ложный эксклюзив на весь бренд
                in_name.append(item.get("item_name") or item.get("collection") or "?")
                continue
            if item.get("where_found") not in WHERE_FOUND:
                bad_place.append(item.get("item_name") or item.get("collection") or "?")
                continue
            claims.append({**item, "supplier": supplier, "price_date": price_date})

        saved = await self._store.record_exclusive_claims(claims)
        active, disputes = resolve(*await self._store.load_exclusives())

        lines = [f"Заявки об эксклюзиве записаны: {saved} (из {len(inp.get('items') or [])})."]
        if in_name:
            lines.append(f"Пропущено — слово входит в само название ({len(in_name)}): "
                         + ", ".join(in_name[:5])
                         + ". Это имя коллекции, а не заявка. Если надпись всё-таки "
                           "стоит отдельно, спроси админа и вызови set_exclusive.")
        if bad_place:
            lines.append(f"Пропущено — не указано, где найдена надпись ({len(bad_place)}): "
                         + ", ".join(bad_place[:5]) + f". Ожидается одно из: {', '.join(WHERE_FOUND)}.")
        for d in disputes:
            lines.append(
                f"⚠️ СПОР: на «{d.title}» эксклюзив заявили несколько поставщиков — "
                + ", ".join(d.suppliers)
                + ". Пометка не показывается, пока админ не выберет. Процитируй ему "
                  "надписи из прайсов ("
                + "; ".join(f"{c.supplier}: «{c.phrase}»" for c in d.claims)
                + f"), спроси, за кем эксклюзив, и вызови set_exclusive (tm_code="
                + f"{d.tm_code}, collection_ref={d.collection_ref or '—'}).")
        if not disputes and saved:
            lines.append("Спорных нет — пометка будет показываться в отчётах.")
        return "\n".join(lines)

    async def _set_exclusive(self, inp: dict) -> str:
        raw = (inp.get("supplier") or "").strip()
        supplier = None if raw.lower() in {"none", "нет", "-", ""} else raw
        await self._store.set_exclusive_decision(
            inp["tm_code"], inp.get("collection_ref") or "", inp.get("item_ref") or "",
            supplier, inp.get("note"))
        if supplier:
            return f"Записано: эксклюзив за «{supplier}». Спрашивать об этом больше не буду."
        return "Записано: эксклюзива нет, пометка снята."

    def _read_price_file(self, inp: dict) -> str:
        """Текст прайса; найденные картинки складывает в self._images (см. _attach)."""
        self._images = []
        if not self._file:
            return "Файл прайса не приложен. Попроси админа прислать файл документом."
        filename, content = self._file
        sheets = parse_price_table(content, filename)
        if not sheets:
            from src.email_tool.attachments import extract_text
            text = extract_text(filename, content)
            return text[:40000] if text else "Не удалось разобрать файл."
        wanted = inp.get("sheet")
        if wanted:
            picked = [s for s in sheets if wanted.lower() in s.name.lower()]
            sheets = picked or sheets

        # Баннер бренда в прайсе часто вставлен картинкой: в тексте на его месте пусто,
        # и раздел другой ТМ выглядит продолжением предыдущей. Ставим маркер и прикладываем
        # само изображение — прочитать логотип может только модель.
        self._images = self._collect_images(content, filename, sheets)
        needle = (inp.get("contains") or "").strip()
        if needle:
            return "\n\n".join(find_rows(sh, needle) for sh in sheets)[:MAX_TEXT_CHARS]
        start = max(1, int(inp.get("from_row") or 1))
        text = "\n\n".join(render_preview(s, start=start) for s in sheets)
        if len(text) > MAX_TEXT_CHARS:
            # раньше текст резался молча, и агент считал, что прайс на этом кончился
            text = text[:MAX_TEXT_CHARS] + (
                "\n\n[Ответ обрезан по объёму — это НЕ конец прайса. Читай листы по "
                "одному (параметр sheet), а длинный лист — частями через from_row.]")
        # текст обрезается по лимиту, и маркер картинки может в него не попасть —
        # тогда изображение приложить не к чему: модель получит его без объяснения
        self._images = [i for i in self._images if f"ИЗОБРАЖЕНИЕ #{i['n']}" in text]
        return text

    def _collect_images(self, content: bytes, filename: str, sheets: list) -> list[dict]:
        if not filename.lower().endswith(".xlsx"):
            return []
        try:
            by_sheet = extract_images(content)
        except Exception:
            logger.warning("Не удалось извлечь изображения прайса", exc_info=True)
            return []
        if not by_sheet:
            return []

        by_name = {s.name: s for s in sheets}
        found: list[dict] = []
        for name, images in by_sheet.items():
            if name in by_name:
                for row, data, media_type in images:
                    found.append({"row": row, "sheet": name, "data": data,
                                  "media_type": media_type})
        found.sort(key=lambda i: (i["sheet"], i["row"]))

        # Один и тот же логотип часто вставлен десятками копий (в прайсе Монарха — 93
        # копии одной картинки, 2.7 МБ). Прикладываем каждую РАЗНУЮ картинку один раз,
        # повторы только ссылаются на неё: иначе лимит съедается копиями одной и той же.
        seen: dict[bytes, dict] = {}
        attached: list[dict] = []
        budget = MAX_IMAGE_TOTAL_BYTES
        for image in found:
            digest = hashlib.sha1(image["data"]).digest()
            first = seen.get(digest)
            if first is not None:
                n = first.get("n")
                image["label"] = (
                    f"⟨ИЗОБРАЖЕНИЕ #{n} ещё раз — та же картинка, что у строки "
                    f"{first['row']}⟩" if n else
                    f"⟨ИЗОБРАЖЕНИЕ — повтор картинки у строки {first['row']}, "
                    "она не приложена⟩")
                continue
            seen[digest] = image
            size = len(image["data"])
            if size > MAX_IMAGE_BYTES:
                image["label"] = (f"⟨ИЗОБРАЖЕНИЕ у строки {image['row']} — {size // 1024} КБ, "
                                  "слишком большое, не приложено⟩")
            elif len(attached) >= MAX_IMAGES or size > budget:
                image["label"] = (f"⟨ИЗОБРАЖЕНИЕ у строки {image['row']} — не приложено, "
                                  f"исчерпан лимит в {MAX_IMAGES} картинок на прайс⟩")
            else:
                budget -= size
                image["n"] = len(attached) + 1
                attached.append(image)
                # Строка — это ЯКОРЬ (левый верхний угол) картинки: визуально она может
                # накрывать и соседние строки, поэтому точную границу раздела модель
                # должна определять по содержимому, а не по позиции маркера.
                image["label"] = (
                    f"⟨ИЗОБРАЖЕНИЕ #{image['n']}, привязано к строке {image['row']} "
                    f"листа «{image['sheet']}» — приложено к этому же результату. "
                    "Картинка может перекрывать соседние строки: точную границу "
                    "раздела определи по заголовкам коллекций и формату артикулов⟩")

        # маркеры вставляем с конца, чтобы не сдвинуть ещё не обработанные строки
        for image in sorted(found, key=lambda i: i["row"], reverse=True):
            sheet = by_name[image["sheet"]]
            idx = min(max(image["row"] - 1, 0), len(sheet.rows))
            sheet.rows.insert(idx, [image["label"]])
        return attached

    def _attach(self, text: str) -> str | list[dict]:
        """Результат инструмента: текст + картинки блоками, если они есть."""
        if not self._images:
            return text
        blocks: list[dict] = [{"type": "text", "text": text}]
        for image in self._images:
            blocks.append({"type": "text",
                           "text": f"Изображение #{image['n']} "
                                   f"(лист «{image['sheet']}», строка {image['row']}):"})
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": image["media_type"],
                "data": base64.b64encode(image["data"]).decode("ascii")}})
        return blocks

    def _selling_tm(self) -> str:
        tms = self._onec.selling_tm()
        _TM_NAMES.update({t.code: t.name for t in tms})   # чтобы звать марки по имени
        return json.dumps([{"name": t.name, "code": t.code} for t in tms], ensure_ascii=False)

    def _nom(self, tm_code: str) -> Nomenclature:
        hit = _NOM_CACHE.get(tm_code)
        if hit and time.monotonic() - hit[0] < NOM_CACHE_TTL:
            return hit[1]
        started = time.monotonic()
        nom = self._onec.by_tm_all(tm_code)
        logger.info("Номенклатура ТМ %s: %d поз. за %.1f c%s", tm_code, len(nom.items),
                    time.monotonic() - started,
                    f", НЕ ОТДАНО 1С: {len(nom.errors)}" if nom.errors else "")
        _NOM_CACHE[tm_code] = (time.monotonic(), nom)
        return nom

    def _items(self, tm_code: str) -> list[NomItem]:
        return self._nom(tm_code).items

    def _nomenclature(self, inp: dict) -> str:
        page, size = int(inp.get("page", 1)), int(inp.get("size", 200))
        nom = self._nom(inp["tm_code"])
        items = nom.items
        # Сужение до коллекции — главный рычаг по токенам: марка на 900 позиций даёт
        # ~45 000 символов на страницу, а в режиме коллекций нужна одна папка из семи.
        wanted = (inp.get("collection_ref") or "").strip()
        if wanted:
            items = [i for i in items if i.collection_ref == wanted]
        chunk = items[(page - 1) * size: page * size]
        return json.dumps({
            "tm": inp["tm_code"], "total": len(items), "page": page,
            "collection_ref": wanted or None,
            # позиции, которые 1С не смогла отдать: их не будет в items, и молчать
            # об этом нельзя — сопоставление с прайсом окажется неполным
            "not_returned_by_1c": [
                {"ref": e.get("ref"), "code": e.get("code"), "message": e.get("message")}
                for e in nom.errors[:20]],
            "items": [{
                "ref": i.ref, "name": i.name, "article": i.article, "size": i.size,
                "collection": i.collection, "collection_ref": i.collection_ref,
                "product_type": i.product_type, "unit": i.unit, "alt_units": i.alt_units,
                "purchase": i.purchase.value if i.purchase else None,
                "retail": i.retail.value if i.retail else None,
                "rrc": i.rrc.value if i.rrc else None,
            } for i in chunk],
        }, ensure_ascii=False)

    # -------------------------------------------------------------- предложение

    def _unproposed(self, inp: dict, scope: list[str], run: dict | None) -> list[str]:
        """ТМ, чью номенклатуру агент грузил, но нигде не учёл.

        Ловит молчаливую потерю целого бренда: в прайсе Монарха так выпал весь лист
        ламината вместе с AGT — `propose_prices` видит только то, что модель ему передала.

        Марки ИЗ ПЛАНА сюда не попадают. При работе по одной ТМ (§9.6) в предложении всегда
        ровно одна, а остальные загруженные либо уже пройдены, либо ждут своей очереди —
        предупреждать о них значит ругаться на нормальный ход прогона. За них отвечает сам
        план: пока в нём есть незакрытые марки, прогон не завершится.
        """
        proposed = {g.get("tm_code") for g in inp.get("groups") or []}
        planned = {t.get("code") for t in (run or {}).get("planned") or []}
        out = []
        for tm_code, (_, nom) in _NOM_CACHE.items():
            if tm_code in proposed or tm_code in planned or not nom.items:
                continue
            kinds = {i.product_type for i in nom.items if i.product_type}
            if kinds and not any(in_scope(scope, k) for k in kinds):
                continue                    # вся ТМ вне анализируемых категорий — так и надо
            colls = {i.collection or i.parent for i in nom.items}
            name = _TM_NAMES.get(tm_code) or tm_code
            out.append(
                f"⚠️ Номенклатуру ТМ {name} ({tm_code}) я загружал — {len(nom.items)} поз., "
                f"{len(colls)} коллекций, — но она не попала ни в план прогона, ни в "
                "предложение. Если это из-за необработанного листа прайса, скажите какого.")
        return out

    async def _propose(self, inp: dict) -> str:
        today = date.today()
        results: list[GroupResult] = []
        problems: list[str] = []
        scope = [c["category"] for c in await self._store.list_scope()]

        # Одна марка за вызов (§9.6): админ смотрит и подтверждает бренд целиком, а не
        # простыню по всему прайсу. Заодно короче отчёт и понятнее, что осталось.
        tms = {g.get("tm_code") for g in inp.get("groups") or [] if g.get("tm_code")}
        if len(tms) > 1:
            names = ", ".join(sorted(tms))
            return (f"Передано несколько ТМ сразу ({names}). Обрабатывай по одной: вызови "
                    "propose_prices только по первой марке, а к следующей переходи после "
                    "того, как админ нажмёт кнопку по этой.")

        run = await self._store.get_run(self._user_id)
        stage = (run or {}).get("stage")
        tm_code = next(iter(tms), None)

        # Крупная марка разбирается по коллекциям (§9.6.2). Порог проверяет КОД: у модели
        # на 900+ позициях не хватает хода, и раньше она отдавала выбор админу вопросом.
        if tm_code and not (stage and stage.get("tm_code") == tm_code):
            nom = await asyncio.to_thread(self._nom, tm_code)
            if len(nom.items) >= BIG_TM_ITEMS:
                colls = {}
                for i in nom.items:
                    colls.setdefault(i.collection_ref or "",
                                     i.collection or i.parent or "без коллекции")
                names = ", ".join(sorted(colls.values())[:15])
                return (f"В этой марке {len(nom.items)} поз. и {len(colls)} коллекций — "
                        "разбираем по коллекциям, целиком за один заход не берём. Вызови "
                        "start_tm_collections, перечислив коллекции марки, которые есть В "
                        f"ПРАЙСЕ (в 1С их: {names}"
                        + (", …" if len(colls) > 15 else "")
                        + "), и дальше передавай propose_prices по одной коллекции.")

        # в режиме коллекций — ровно одна за вызов, симметрично правилу «одна ТМ»
        if stage and stage.get("tm_code") == tm_code:
            refs = {g.get("collection_ref") for g in inp.get("groups") or []}
            if len(refs) > 1:
                return ("Сейчас марка разбирается по коллекциям — передавай в "
                        "propose_prices ОДНУ коллекцию за вызов. Порядок: "
                        + ", ".join(c["name"] for c in stage["remaining"]))

        seen_tm: set[str] = set()
        for g in inp.get("groups", []):
            nom = await asyncio.to_thread(self._nom, g["tm_code"])
            # 1С может не отдать часть позиций (см. specs/1c/by-tm.bsl): они не попадут
            # в сопоставление, и админ должен увидеть это в предложении, а не догадываться
            if nom.errors and g["tm_code"] not in seen_tm:
                refs = ", ".join(str(e.get("ref")) for e in nom.errors[:5] if e.get("ref"))
                problems.append(
                    f"⚠️ 1С не отдала {len(nom.errors)} поз. по ТМ "
                    f"{g.get('tm_name') or g['tm_code']} — они НЕ проверены по прайсу"
                    + (f" ({refs}{', …' if len(nom.errors) > 5 else ''})" if refs else ""))
            seen_tm.add(g["tm_code"])
            items = nom.items
            sel = [i for i in items if i.collection_ref == g.get("collection_ref")]
            if not sel:
                problems.append(f"коллекция {g.get('collection_ref')} не найдена у ТМ {g['tm_code']}")
                continue
            # категория вне анализа — не пишем, даже если модель коллекцию передала (§6.8)
            kind = next((i.product_type for i in sel if i.product_type), None)
            if not in_scope(scope, kind):
                problems.append(
                    f"⚠️ {sel[0].collection or g.get('collection_ref')} ({kind}) — категория "
                    "не в списке анализируемых, цены не трогаю. Изменить: /categories")
                continue
            per_item = g.get("items") or []
            if per_item:
                # плитка: в одной папке настенная, напольная, декоры — у каждого своя цена
                group, missing = plan_items(sel, g["tm_code"], g.get("tm_name", ""),
                                            per_item, today)
                if missing:
                    problems.append(
                        f"⚠️ {sel[0].collection or g.get('collection_ref')}: не нашёл в 1С "
                        f"{len(missing)} поз. из переданных ({', '.join(missing[:5])}"
                        + (", …" if len(missing) > 5 else "") + ") — цены по ним не тронуты.")
                if group.plans:
                    results.append(group)
                continue
            results.append(plan_collection(
                sel, g["tm_code"], g.get("tm_name", ""),
                _dec(g.get("purchase")), _dec(g.get("rrc")), today))

        problems += self._unproposed(inp, scope, await self._store.get_run(self._user_id))
        payload = build_payload(results)
        # новое предложение отменяет прежнее неподтверждённое, и старая кнопка перестаёт
        # работать — молчать об этом нельзя, админ решит, что запись просто сломалась
        if payload:
            previous = await self._store.get_pending(self._user_id)
            if previous:
                problems.append(
                    f"⚠️ Прежнее предложение ({previous.item_count} поз.) отменяется — "
                    "кнопка под ним больше не сработает. Если его нужно было записать, "
                    "скажите: соберу заново.")

        active, _ = resolve(*await self._store.load_exclusives())
        summary = self._render(inp, results, problems, payload, active)
        self.last_summary = summary

        coll_ref = next((g.get("collection_ref") for g in inp.get("groups") or []), None)
        in_stage = bool(stage and stage.get("tm_code") == tm_code)

        if payload:
            await self._store.save_proposal(self._user_id, payload, summary,
                                            digest=self._digest(inp, results))
            run = await self._store.get_run(self._user_id)
            # Текст остаётся здесь, чтобы модель знала, что предложила, и могла ответить
            # на уточняющий вопрос админа: в истории он кешируется и стоит копейки.
            # Дорого обходилось другое — модель ПЕРЕПИСЫВАЛА его на выход (~2 000 токенов
            # на шаг, у крупного предложения 8 500, по цене выхода) и попутно теряла
            # строки: так уже пропадало «Осталось обработать». Теперь текст админу шлёт
            # обработчик из last_summary.
            self.last_summary = summary + queue_tail(run, tm_code,
                                                     coll_ref if in_stage else None)
            return (self.last_summary
                    + "\n\n[Предложение сохранено, и этот текст я уже показал админу — "
                      "ПЕРЕПИСЫВАТЬ ЕГО НЕ НУЖНО. Ответь одной короткой фразой, только "
                      "если есть что добавить сверх предложения, иначе ответь пустой "
                      "строкой. Жди кнопки, дальше по очереди сам не иди.]")

        # писать нечего — подтверждать нечего, шаг закрываем сразу и идём дальше
        if in_stage and coll_ref:
            run = await self._store.mark_collection_done(self._user_id, coll_ref)
        elif tm_code:
            run = await self._store.mark_tm_done(self._user_id, tm_code)
        else:
            run = None
        nxt = next_step(run)
        if nxt:
            self.advanced_to = nxt
            return (summary + queue_tail(run)
                    + f"\n\n[Записывать нечего, кнопки не будет. Покажи текст админу "
                      f"и сразу продолжай: {nxt}.]")
        # итоговый блок и выход из режима печатает обработчик — он один на оба пути
        return summary + "\n\n[Записывать нечего — кнопка подтверждения не появится.]"

    def _digest(self, inp: dict, results: list[GroupResult]) -> dict:
        """Снимок «было → стало» по товарам — для рассылки менеджерам и журнала (п.6).

        Сохраняется вместе с payload: после нажатия кнопки пересчитать его уже неоткуда,
        а «было» есть только на нашей стороне.
        """
        return {
            "supplier": inp.get("supplier"),
            "price_doc": inp.get("price_doc") or (self._file[0] if self._file else None),
            "price_date": inp.get("price_date"),
            "groups": [{
                "tm_code": g.tm_code, "tm_name": g.tm_name,
                "collection": g.collection, "collection_ref": g.collection_ref,
                "items": [{
                    "ref": p.ref, "name": p.name,
                    "prices": {k: [None if p.before.get(k) is None else float(p.before[k]),
                                   float(v)] for k, v in p.prices.items()},
                } for p in g.to_write],
            } for g in results if g.to_write],
        }

    def _render(self, inp: dict, results: list[GroupResult], problems: list[str],
                payload: list[dict], exclusives: dict | None = None) -> str:
        supplier = inp.get("supplier") or "поставщика"
        lines = [f"Обновляем цены от {supplier} на:"]
        by_tm: dict[str, list[GroupResult]] = {}
        for g in results:
            by_tm.setdefault(g.tm_name or g.tm_code, []).append(g)

        total_items = 0
        for tm_name, groups in by_tm.items():
            lines.append(f"— {tm_name}")
            untouched: list[str] = []
            for g in groups:
                rows = (_transitions(g, "purchase", "закупка")
                        + _transitions(g, "rrc", "РРЦ")
                        + _transitions(g, "retail", "розница"))
                if not rows:
                    # коллекции без изменений не перечисляем по одной: в прайсе их
                    # обычно большинство, и они прячут собой то, что реально меняется
                    untouched.append(g.collection)
                    continue
                total_items += len(g.to_write)
                title = annotate(g.collection,
                                 find(exclusives or {}, g.tm_code, g.collection_ref))
                lines.append(f"  • {title} — {len(g.plans)} поз.")
                for r in rows:
                    lines.append(f"      {r}")
            if untouched:
                shown = ", ".join(untouched[:6])
                tail = f" и ещё {len(untouched) - 6}" if len(untouched) > 6 else ""
                lines.append(f"  • без изменений ({len(untouched)}): {shown}{tail}")
        lines.append("?")

        # скачки, похожие на перепутанную ЕИ, идут ПЕРВЫМИ: среди трёх десятков строк
        # «+102%» теряется, а это самая дорогая ошибка режима (§9.1.1)
        suspicious: list[str] = []
        for g in results:
            suspicious += unit_warnings(g)
        warns = suspicious + list(inp.get("warnings") or []) + problems
        for g in results:
            below_p = [p for p in g.plans if p.warning == "rrc_below_purchase"]
            below_r = [p for p in g.plans if p.warning == "rrc_below_retail"]
            skipped = sum(1 for p in g.plans if "below_threshold" in p.skipped.values())
            if below_p:
                warns.append(f"⚠️ {g.collection}: РРЦ ниже закупки — проверьте прайс ({len(below_p)} поз.)")
            if below_r:
                warns.append(f"⚠️ {g.collection}: наша розница выше РРЦ ({len(below_r)} поз.) — цену не меняю")
            if skipped:
                warns.append(f"ℹ️ {g.collection}: {skipped} поз. пропущено — изменение меньше 2%")
        if warns:
            lines.append("")
            lines.extend(warns)

        lines.append("")
        lines.append(f"К записи: {total_items} поз. ({len(payload)} строк запроса в 1С).")
        return "\n".join(lines)
