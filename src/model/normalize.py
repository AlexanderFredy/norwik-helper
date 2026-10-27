"""Нормализация наименований — КОДОМ, без обращения к модели (§6.2, вид «нормализация»).

**ПОЧЕМУ ИМЕННО ЭТОТ ВИД ЗАДАЧ РЕШАЕТСЯ БЕЗ LLM.** У него нет ни одного входа снаружи 1С:
прайс для нормализации не нужен вовсе. Всё, из чего собирается наименование, уже лежит в
карточке — вид товара, марка, коллекция, название расцветки (`site_name` по §19.5) и
размер. Сборка — существующий `build_name`, сравнение с текущим именем — сравнение строк.
Решать здесь нечего, а значит платить за круг модели ($0.097 за вызов инструмента) и
терпеть её непредсказуемость незачем.

Остальным видам задач прайс нужен: «добавить новые» требует сопоставить строки файла с
номенклатурой, «перенести в снятые» — понять, чего в прайсе не стало. Это суждение, и оно
остаётся за агентом.

**ПРАВИЛА НЕ ПЕРЕПИСЫВАЮТСЯ.** Модуль не собирает имена сам: он готовит РОВНО ТОТ ЖЕ вход,
который построила бы модель, только из данных 1С, — и отдаёт его `items.plan_collection`.
Оттуда приходят и сборка имени, и вычистка своего артикула, и проверка на запрещённые
символы, и предупреждение о разъехавшейся коллекции. Второй набор правил разошёлся бы с
первым молча: запись прошла бы, а имена вышли бы другими.

**ЧТО МОЛЧА, ЧТО АДМИНУ.** Приведение имён к шаблону — молча, это и есть работа. Админу
достаётся лишь то, чего код решить не вправе: позиции без названия расцветки (выдумывать
его он не будет) и позиции, у которых само название загрязнено — в него затесались марка
или коллекция. Такие не чинятся, иначе грязь размножилась бы по наименованиям.
"""
from __future__ import annotations

import logging
import re

from src.price_tool.naming import collection_from_folder
from src.price_tool.scope import normalize as _norm

logger = logging.getLogger(__name__)


def _form(value: str | None) -> str:
    """Сравнение имён без учёта регистра и пунктуации — та же `scope.normalize`, что
    и везде в проекте: своё правило сравнения разошлось бы с чужим на пустом месте."""
    return _norm(value or "")


class Skipped:
    """Причины, по которым позиция не нормализуется. Значения — текст для админа."""
    NO_TITLE = "нет названия расцветки (site_name пуст) — вывести его неоткуда"
    DIRTY_TITLE = "в названии расцветки марка или коллекция — сначала почистить его"
    NO_COLLECTION = "не заполнена коллекция — имя собрать не из чего"
    SAME_NAME = ("в названии расцветки только коллекция, а размер у соседней позиции тот же "
                 "— без расцветки имена совпали бы")


#: Код свойства «Коллекция» в 1С. Выведенное из папки имя НИКОГДА не записывается в него —
#: см. `collection_of` и `strip_collection_property`.
COLLECTION_PROPERTY = "0000003"


def strip_collection_property(inp: dict) -> tuple[dict, str]:
    """Убрать из правки попытку записать СВОЙСТВО «Коллекция». Возвращает (правка, заметка).

    **ПРАВИЛО: пустое свойство «Коллекция» НЕ заполняется именем папки.** Папка и свойство
    — разные вещи. Имя папки у восьми напольных категорий несёт размер (§19.5), содержит
    служебные пометки и меняется при пересортировке справочника; свойство же попадает на
    сайт как признак коллекции. Переписать одно другим значит подменить справочные данные
    производной величиной, выведенной для сборки строки.

    Выведенное из папки имя годится РОВНО НА ОДНО — подставиться в шаблон наименования.
    Заполнять им реквизит вправе только человек, который знает, как коллекция называется
    на самом деле.

    Запрет стоит у самой записи, а не в промпте: `get_1c_items` отдаёт коллекцию уже
    выведенной, и модель, добросовестно увидев её в выгрузке, может вернуть то же значение
    свойством.
    """
    # Свойства лежат ВНУТРИ КАЖДОЙ ПОЗИЦИИ (`items[].properties`), а не на верхнем уровне
    # правки: `plan_collection` читает их у каждой строки отдельно.
    rows = inp.get("items")
    if not isinstance(rows, list):
        return inp, ""

    dropped = 0
    out_rows = []
    for row in rows:
        props = (row or {}).get("properties")
        if not isinstance(props, list):
            out_rows.append(row)
            continue

        kept = [p for p in props
                if str((p or {}).get("property", "")).strip() != COLLECTION_PROPERTY]
        if len(kept) == len(props):
            out_rows.append(row)
            continue

        dropped += len(props) - len(kept)
        clean = dict(row)
        clean["properties"] = kept
        out_rows.append(clean)

    if not dropped:
        return inp, ""

    out = dict(inp)
    out["items"] = out_rows
    return out, (f"свойство «Коллекция» не заполняется автоматически ({dropped} поз.): "
                 "имя папки для этого не годится, значение вправе поставить только админ")


def collection_of(item) -> str:
    """Имя коллекции ДЛЯ НАИМЕНОВАНИЯ ТОВАРА, а не для показа и НЕ для записи в свойство.

    Свойство «Коллекция» заполнено — берём его. Пустое (у A+ Floor такими оказались все
    двадцать позиций) — остаётся имя папки, и вот тут ловушка: у восьми напольных
    категорий в имени папки СТОИТ РАЗМЕР по §19.5. Подставив его как коллекцию, мы
    получаем «Ламинат A+ Floor Ле Паркет 600x600x14 Авила» — размер в середине
    наименования, где его быть не должно: в имя товара он идёт только у керамики.

    Поэтому хвост-размер снимается точной обратной операцией к `folder_name`.

    **Выведенное значение НЕ записывается в свойство «Коллекция».** Оно производное: имя
    папки меняется при пересортировке справочника и несёт размер. Пустое свойство остаётся
    пустым, пока его не заполнит человек (`strip_collection_property`).
    """
    own = (item.collection or "").strip()
    if own:
        return own
    return collection_from_folder(item.parent, item.size)


def group_key(item) -> tuple[str, str]:
    """По чему бьём на вызовы `plan_collection`: она работает на одну коллекцию и вид."""
    return (collection_of(item), (item.product_type or "").strip())


def contaminated(item, tm_name: str, collection: str) -> bool:
    """Загрязнено ли название расцветки.

    ЗАЧЕМ ЭТА ПРОВЕРКА. Канон собирается как `[вид] [марка] [коллекция] [название]`. Если
    марка или коллекция уже сидят ВНУТРИ названия расцветки, они попадут в имя дважды:
    «Ламинат Peli Anatolia Peli Anatolia Дуб». Механически это видно, и чинить такое
    вслепую нельзя — неизвестно, что из этого лишнее, а что часть настоящей расцветки.
    """
    site = _form(getattr(item, "site_name", ""))
    if not site:
        return False
    for other in (tm_name, collection):
        probe = _form(other)
        # Короткие куски не проверяем: «Про» или «Ле» встречаются внутри расцветок
        # как обычные слова, и запрет по ним выключил бы нормализацию целым коллекциям.
        if len(probe) >= 4 and probe in site:
            return True
    return False


def strip_prefix(title: str, tm_name: str, collection: str) -> str:
    """Снять марку и коллекцию с НАЧАЛА названия расцветки. Пусто — снимать нечего или
    после снятия не осталось названия.

    **ЗАЧЕМ (просьба админа 11.10.2026, задача 996).** У Atlas Concorde 273 позиции стояли
    в «требует решения» с одной и той же картиной: коллекция «Mark», расцветка «MARK
    IVORY», «MARK TAUPE MOSAIC» — поставщик начинал название расцветки с имени коллекции.
    Админ дописал в задачу «почисти», но нормализацию выполняет КОД, описание он не читает,
    и ответ не менялся.

    **ТОЛЬКО НАЧАЛО И ТОЛЬКО ЦЕЛЫЕ СЛОВА.** В начале имя коллекции или марки — это повтор
    шаблона `[марка] [коллекция] [название]`, снимать его безопасно. Внутри названия
    («Дуб Mark Светлый») мы поняли бы строку неверно — такое по-прежнему решает человек.
    Марка берётся обеими половинами двуязычного имени: «Atlas Concorde / Атлас конкорд».
    """
    words = str(title or "").split()
    if not words:
        return ""
    prefixes = []
    for name in [collection, *str(tm_name or "").split("/"), tm_name]:
        parts = [w.casefold() for w in str(name or "").split()]
        if parts and len("".join(parts)) >= 3:
            prefixes.append(parts)
    # Сначала длинные: «Boost Stone» раньше «Boost».
    prefixes.sort(key=len, reverse=True)

    changed = True
    stripped = False
    while changed and words:
        changed = False
        low = [w.casefold() for w in words]
        for parts in prefixes:
            if low[:len(parts)] == parts and len(words) > len(parts):
                words = words[len(parts):]
                changed = stripped = True
                break
    return " ".join(words) if stripped else ""


def _dirty(title: str, tm_name: str, collection: str) -> bool:
    """Осталось ли в очищенном названии марки или коллекции (та же проверка, что у карточки)."""
    return contaminated(type("Probe", (), {"site_name": title})(), tm_name, collection)


#: Слова видов товара — их в названии расцветки быть не может; встретилось — в поле лежит
#: целое наименование, и чистить его кусками нельзя.
_TYPE_WORDS = ("плитка", "керамогранит", "ламинат", "паркет", "доска", "линолеум", "обои")


def _foreign(title: str, product_type: str, marks=()) -> bool:
    """Несёт ли название вид товара или имя чужой марки — признак целого наименования."""
    words = {w.casefold().strip(".,;:()") for w in str(title or "").split()}
    if any(w in words for w in _TYPE_WORDS):
        return True
    if product_type and _form(product_type) in _form(title):
        return True
    return bool(words & mark_words(marks))


def strip_collection(title: str, collection: str) -> str:
    """Снять имя КОЛЛЕКЦИИ из любого места названия, целыми словами. Пусто — нечего
    снимать или не осталось ничего.

    **ЗАЧЕМ (решение админа 27.10.2026, нормализация Porcelanosa).** У плитки поставщик
    пишет `[элемент] [коллекция] [цвет]`: «STRIPE TARANTO GOLD», «DECO TERRA TOPO», «LISTON
    OXFORD NATURAL». Коллекция тут не в начале, и `strip_prefix` её не брал — 12 позиций
    уходили админу. Тип элемента (Stripe, Deco, Listón) значим: им бордюр отличается от
    основной плитки, — поэтому снимается ТОЛЬКО коллекция, остальное остаётся.

    Марку внутри названия по-прежнему решает человек: «Venis» в имени Porcelanosa — это
    след переноса, а не повтор шаблона.
    """
    words = str(title or "").split()
    parts = [w.casefold() for w in str(collection or "").split()]
    if not words or not parts or len("".join(parts)) < 3:
        return ""
    low = [w.casefold().strip(".,;:") for w in words]
    out, i, stripped = [], 0, False
    while i < len(words):
        if low[i:i + len(parts)] == parts:
            i += len(parts)
            stripped = True
            continue
        out.append(words[i])
        i += 1
    return " ".join(out) if stripped and out else ""


def title_is_collection(title: str, tm_name: str, collection: str) -> bool:
    """В названии расцветки нет НИЧЕГО, кроме коллекции (и, может быть, марки): «Persia»."""
    rest = {w.casefold().strip(".,;:") for w in str(title or "").split()}
    names = {w.casefold() for name in [collection, *str(tm_name or "").split("/")]
             for w in str(name or "").split()}
    return bool(rest) and rest <= names and any(
        w.casefold() in rest for w in str(collection or "").split())


def mark_words(marks) -> set[str]:
    """Слова, которыми зовутся МАРКИ, — из них и состоят заводские пометки.

    Имя марки приходит как в 1С: «Westerhof / Вестерхоф», «A+ Floor». Разбираем на слова,
    потому что в прайсе пометка стоит одним: «Альфа PELI», «Завод AGT». Короткие куски
    («A+», «/») отбрасываем — они совпали бы со случайными словами названий.
    """
    out = set()
    for mark in marks or ():
        for word in str(mark or "").replace("/", " ").split():
            key = word.casefold().strip(".,;")
            if len(key) >= 3:
                out.add(key)
    return out


def shared_tail(titles, marks=()) -> set[str]:
    """Хвостовые слова, повторяющиеся у РАЗНЫХ расцветок коллекции И называющие МАРКУ.

    **СЛУЧАЙ С БОЯ (24.09.2026).** Westerhof делает часть коллекций на заводе Peli, часть
    на AGT, и поставщик дописал это в каждое наименование: «Альфа PELI», «WHITE PELI»,
    «Альпы AGT». Завод — свойство коллекции, а не расцветки: в имени товара он повторяется
    у всех и не различает ничего.

    **ОДНОГО ПОВТОРА МАЛО, И ЭТО ВТОРОЙ СЛУЧАЙ С БОЯ (28.09.2026).** Сначала шумом
    считалось любое слово, повторившееся не на первом месте, а род защищался тем, что
    стоит спереди: «Дуб Авила», «Дуб Прато». Но у Peli род пишут В КОНЦЕ — «Белый Дуб»,
    «Дымчатый Дуб», — и нормализация оставила «Белый» и «Дымчатый», потеряв породу
    дерева в наименовании и на сайте. Положение слова разделяет эти случаи не всегда.

    Разделяет их ПРОИСХОЖДЕНИЕ: шум, ради которого правило писалось, — это ИМЯ МАРКИ
    (завода). Марки известны из 1С, и сверка с ними отвечает на вопрос точно, а не по
    приметам. «Дуб» маркой не зовётся нигде, «PELI» и «AGT» зовутся.

    Без списка марок правило МОЛЧИТ. Не снять лишнее слово — потеря на глаз, её видно и
    поправимо; снять нужное — потеря молчаливая, и находится она через неделю в карточке.

    **Достаточно двух совпадений, а не всех.** У коллекции «Effect» пометка AGT стоит лишь
    у семи позиций из четырнадцати — поставщик проставил её не везде. Требуй мы совпадения
    у всех, самый очевидный случай и не сработал бы.
    """
    known = mark_words(marks)
    if not known:
        return set()

    rows = [str(t or "").split() for t in titles]
    rows = [r for r in rows if len(r) > 1]
    if len(rows) < 2:
        return set()

    seen: dict[str, int] = {}
    for row in rows:
        # Первое слово — вне игры и теперь: род спереди («Дуб Авила») встречается чаще,
        # чем марка на первом месте, а спорные случаи лучше оставлять как есть.
        for word in dict.fromkeys(row[1:]):
            key = word.casefold()
            if key in known:
                seen[key] = seen.get(key, 0) + 1

    return {key for key, times in seen.items() if times >= 2}


def drop_shared(title: str, noise: set[str]) -> str:
    """Снять шумовые слова с ХВОСТА названия. Середину не трогаем.

    Снимается только то, что стоит с краю: «Альфа PELI» → «Альфа». Слово внутри имени
    («Дуб PELI Медовый») осталось бы на месте — такое написание означает, что мы поняли
    строку неверно, и молча кромсать середину опаснее, чем оставить как есть.
    """
    words = str(title or "").split()
    while len(words) > 1 and words[-1].casefold() in noise:
        words.pop()
    return " ".join(words)


def plan(items, tm_code: str, tm_name: str, only_collection: str = "",
         marks=(), notes: dict | None = None):
    """Что нормализуем и о чём докладываем.

    Возвращает `(входы, пропуски)`: входы — готовые словари для `plan_collection`,
    пропуски — `(позиция, причина)` для отчёта админу.

    `only_collection` сужает работу до одной коллекции: задача чаще всего адресована ей,
    а первая настоящая запись должна быть такой, чтобы её можно было проверить глазами.
    """
    wanted = _form(only_collection)
    groups: dict[tuple[str, str], list] = {}
    skipped: list[tuple[object, str]] = []
    discontinued = 0
    # Название расцветки, с которым позиция пойдёт в план: обычно `site_name` как есть,
    # а у очищенных — без марки и коллекции в начале (`strip_prefix`).
    titles: dict[str, str] = {}
    cleaned: dict[str, int] = {}
    # Позиции, у которых расцветки нет вовсе — только коллекция (см. ниже).
    decorless: set[str] = set()

    for item in items:
        # СНЯТЫЕ НЕ ТРОГАЕМ. Они лежат в невыгружаемых папках, на сайт не идут, и
        # переименование им ничего не даёт — зато раздувает пачку записи.
        if getattr(item, "not_exported", False):
            discontinued += 1
            continue

        collection, product_type = group_key(item)
        if wanted and _form(collection) != wanted:
            continue

        if not collection:
            skipped.append((item, Skipped.NO_COLLECTION))
            continue

        title = (getattr(item, "site_name", "") or "").strip()
        if not title:
            skipped.append((item, Skipped.NO_TITLE))
            continue

        if contaminated(item, tm_name, collection):
            # МАРКА ИЛИ КОЛЛЕКЦИЯ В НАЧАЛЕ — снимаем сами. Осталось грязным после снятия
            # (повтор внутри названия) — решает человек, как и прежде.
            #
            # СВОЙ АРТИКУЛ ВПЕРЕДИ НИХ снимается первым: у Boost 96 названий вида «8B8P
            # Boost Pearl» — без этого коллекция стояла бы уже не в начале.
            from src.price_tool.naming import drop_own_article, size_in_name
            base = drop_own_article(title, item.article) or title
            clean = strip_prefix(base, tm_name, collection)
            # ОБА ПРАВИЛА НИЖЕ — ТОЛЬКО ДЛЯ КЕРАМИКИ (керамическая плитка, керамогранит;
            # решение админа 27.10.2026). Это те же виды, у которых размер стоит в имени
            # (`size_in_name`): у плитки название вида `[элемент] [коллекция] [цвет]`, а у
            # ламината коллекция внутри названия — случай, где мы скорее поняли строку
            # неверно, и решает по-прежнему человек.
            ceramic = size_in_name(product_type,
                                   getattr(item, "product_type_ref", "") or None)
            if not clean or _dirty(clean, tm_name, collection):
                # КОЛЛЕКЦИЯ ВНУТРИ НАЗВАНИЯ — снимаем и её (решение админа 27.10.2026):
                # «STRIPE TARANTO GOLD» → «STRIPE GOLD». Марку внутри — нет.
                inner = strip_collection(clean or base, collection) if ceramic else ""
                # ОСТАТОК НЕ ДОЛЖЕН НЕСТИ ВИД ТОВАРА ИЛИ ЧУЖУЮ МАРКУ (бой 27.10.2026,
                # Dayton): в поле лежало старое полное имя «Керамическая плитка Venis
                # Dayton Croix Sand», и сняв одну коллекцию, мы собирали «…Dayton
                # Керамическая плитка Venis Croix Sand». Своя марка ловится `_dirty`, а
                # чужую и вид товара он не знает — проверяем здесь.
                if inner and _foreign(inner, product_type, marks):
                    inner = ""
                if inner and not _dirty(inner, tm_name, collection):
                    clean = inner
                elif (ceramic and title_is_collection(base, tm_name, collection)
                      and (item.size or "").strip()):
                    # ТОЛЬКО КОЛЛЕКЦИЯ — расцветки нет вовсе (решение админа 27.10.2026):
                    # у керамики товар различает РАЗМЕР в наименовании, «Persia» →
                    # «Керамическая плитка Porcelanosa Persia 33,3x100».
                    decorless.add(item.ref)
                    clean = ""
                else:
                    skipped.append((item, Skipped.DIRTY_TITLE))
                    continue
            removed = (title[: len(title) - len(clean)].strip()
                       if clean and title.endswith(clean) else title)
            # В отчёт — без своего артикула: он у каждой позиции свой, и список «8B8P Boost,
            # AMH4 Boost, …» ничего бы не сказал админу.
            key = drop_own_article(removed, item.article) or removed
            cleaned[key] = cleaned.get(key, 0) + 1
            title = clean

        titles[item.ref] = title
        groups.setdefault((collection, product_type), []).append(item)

    # БЕЗ РАСЦВЕТКИ ТОВАР РАЗЛИЧАЕТ ТОЛЬКО РАЗМЕР — значит, он не должен совпасть с соседним
    # в той же коллекции и виде. Совпал — имена вышли бы одинаковыми; решает человек.
    for key, members in list(groups.items()):
        sizes: dict[str, int] = {}
        for i in members:
            sizes[_form(i.size)] = sizes.get(_form(i.size), 0) + 1
        clash = [i for i in members if i.ref in decorless and sizes[_form(i.size)] > 1]
        for i in clash:
            skipped.append((i, Skipped.SAME_NAME))
            decorless.discard(i.ref)
        groups[key] = [i for i in members if i not in clash]
        if not groups[key]:
            del groups[key]

    inputs = []
    found_noise: set[str] = set()
    for (collection, product_type), members in groups.items():
        first = members[0]
        # Шум считается ПО КОЛЛЕКЦИИ целиком: одна позиция о повторе ничего не знает.
        noise = shared_tail((titles[i.ref] for i in members), marks)
        found_noise.update(noise)
        inputs.append({
            "tm_code": tm_code,
            # Имя марки — из 1С. Ради этого модуль и не спрашивает никого: как пишется
            # марка, решает справочник.
            "tm_name": tm_name,
            "product_type": getattr(first, "product_type_ref", "") or product_type,
            "product_type_name": product_type,
            "collection": collection,
            "items": [{
                "op": "update",
                "ref": i.ref,
                "article": i.article,
                # `site_name` по §19.5 — это РОВНО название расцветки, без размера и без
                # артикула. То есть готовый `title`, который модель собирала бы вручную.
                "title": drop_shared(titles[i.ref], noise),
                # Размер отдаём как есть: нужен он в имени или нет, решит
                # `plan_collection` по виду товара (`size_in_name`), а не мы.
                "tail": (i.size or "").strip(),
                # Пустая расцветка ОСОЗНАННО: без этого признака `plan_collection` пустое
                # название отвергает (случай 10.09.2026 — имена всей коллекции схлопнулись
                # бы в одно).
                **({"no_decor": True} if i.ref in decorless else {}),
            } for i in members],
        })

    if notes is not None:
        notes["cleaned"] = cleaned
    return inputs, skipped, discontinued, found_noise


def pending_work(items, tm_code: str, tm_name: str, scope=None) -> int:
    """Сколько правок дала бы нормализация прямо сейчас. Ноль — делать нечего.

    **ЗАЧЕМ СЧИТАТЬ ЗАРАНЕЕ.** Задача заводится на марку целиком и никакой проверки до
    сих пор не проходила — в отличие от остальных видов, где есть `compare_with_1c`. На
    Most Flooring это вылезло сразу: восемь коллекций, сто позиций, и НИ ОДНОЙ правки —
    имена давно приведены. Админ получил задачу, открыл, выполнил и увидел «менять было
    нечего». Такие задачи обесценивают список: их перестают читать вместе с настоящими.

    Считается ТЕМ ЖЕ `plan_collection`, что и выполнит работу, — иначе оценка разошлась бы
    с делом. Дорого это не стоит: выгрузка уже лежит в кеше прогона, модель не участвует.
    """
    from src.price_tool import items as item_rules

    inputs, skipped, _, _ = plan(items, tm_code, tm_name)
    # Пропущенные — это тоже работа: админу есть что решить, и задача нужна.
    if skipped:
        return len(skipped)

    total = 0
    for inp in inputs:
        total += len(item_rules.plan_collection(inp, list(items), list(scope or [])).ops())
    return total


def report(written: int, skipped: list, discontinued: int, groups: int,
           noise: set[str] | None = None, cleaned: dict | None = None) -> str:
    """Короткий отчёт админу: сделанное одной строкой, разбирательства — списком."""
    lines = []
    if written:
        lines.append(f"Наименования приведены к шаблону: {written} поз. "
                     f"в {groups} колл.")
    else:
        lines.append("Расхождений с шаблоном не нашлось — менять было нечего.")

    if discontinued:
        lines.append(f"Снятые с производства не трогал: {discontinued} поз.")

    # ПОЧЕМУ ИМЕНА СТАЛИ КОРОЧЕ. Без этой строки админ видит переименования и гадает,
    # куда делось слово. Сами слова называются: решение «шум это или нет» принимал код,
    # и проверить его должно быть можно с одного взгляда.
    if noise:
        lines.append("Сняты повторяющиеся слова в конце названий (маркер коллекции, "
                     "а не расцветки): " + ", ".join(sorted(noise)) + ".")
    # СНЯТОЕ В НАЧАЛЕ — НАЗЫВАЕТСЯ: решение принимал код, и админ должен видеть, что
    # именно ушло из названий, — «MARK IVORY» стало «Ivory» не само по себе.
    if cleaned:
        shown = ", ".join(f"«{k}» — {v} поз." for k, v in
                          sorted(cleaned.items(), key=lambda kv: -kv[1])[:10])
        lines.append(f"Сняты марка/коллекция в начале названий расцветки: {shown}.")

    if skipped:
        by_reason: dict[str, list] = {}
        for item, reason in skipped:
            by_reason.setdefault(reason, []).append(item.article or item.ref)
        lines.append("")
        lines.append("ТРЕБУЕТ ВАШЕГО РЕШЕНИЯ:")
        for reason, refs in by_reason.items():
            shown = ", ".join(refs[:8])
            more = f" и ещё {len(refs) - 8}" if len(refs) > 8 else ""
            lines.append(f"— {len(refs)} поз.: {reason} ({shown}{more})")

    return "\n".join(lines)


# Хвост имени папки вида «600x238x12»: цифры через x, ×, х (латинская и кириллическая).
_SIZE_TAIL = re.compile(r"\s+\d+(?:[x×х]\d+)+$", re.IGNORECASE)


def collection_keys(name: str, tm: str = "") -> set[str]:
    """Под какими именами искать эту папку. Вариантов три, и все нужны.

    У восьми напольных категорий размер пишется в имя ПАПКИ (§19.5): «Классик 600x238x12».
    Коллекция же зовётся «Классик» — и в прайсе, и в свойстве «Коллекция», и в адресе
    задачи. По полному имени папки такая коллекция не находилась никогда, из-за чего
    сверка марок (`fix_marks`) молча не срабатывала на всей марке A+ Floor.

    Хвост снимается только СТРОГО ПОХОЖИЙ на размер: цифры, разделённые «x». «Формат 3D»
    под это не попадает — там нет второго числа.

    **Третий вариант — БЕЗ ПРИСТАВКИ МАРКИ**, и он из боя 25.09.2026. В прайсе Вестерхофа
    коллекции зовутся «Westerhof Spark», «Westerhof Vivace», а в 1С свойство «Коллекция»
    и папка — просто «Spark», «Vivace». Совпадения не было ни одного, сверка отвечала
    «коллекции в 1С нет», и агент предлагал завести заново три ЖИВЫЕ коллекции: 8, 8 и 10
    позиций. Приставка снимается только ЦЕЛЫМ СЛОВОМ и только с начала: «Spark» внутри
    «Sparkling» не тронется, а «Modern» у другой марки не притянется, потому что снимаем
    имя ИМЕННО ЭТОЙ марки. Двуязычное «Westerhof / Вестерхоф» даёт оба слова.
    """
    text = " ".join(str(name or "").split())
    variants = [text, _SIZE_TAIL.sub("", text)]

    for word in _tm_words(tm):
        for variant in list(variants):
            flat = variant.strip()
            low = flat.lower()
            if low.startswith(word) and len(flat) > len(word):
                tail = flat[len(word):].strip(" -–—/")
                if tail:
                    variants.append(tail)

    return {_norm(v) for v in variants} - {""}


def _tm_words(tm: str) -> list[str]:
    """Слова, которыми марка может начинать имя коллекции: «Westerhof / Вестерхоф» → оба.

    Только ПЕРВОЕ слово каждой половины: марки вроде «A+ Floor» состоят из двух, но в
    приставке прайса стоит обычно одно. Лишнее слово тут безвреднее недостающего —
    снятие приставки проверяется по началу строки и по длине остатка.
    """
    out = []
    for half in str(tm or "").split("/"):
        word = half.strip().lower()
        if word:
            out.append(word)
            first = word.split()[0]
            if first != word:
                out.append(first)
    return out
