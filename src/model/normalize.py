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


def shared_tail(titles) -> set[str]:
    """Хвостовые слова, повторяющиеся у РАЗНЫХ расцветок коллекции, — это шум.

    **СЛУЧАЙ С БОЯ (24.09.2026).** Westerhof делает часть коллекций на заводе Peli, часть
    на AGT, и поставщик дописал это в каждое наименование: «Альфа PELI», «WHITE PELI»,
    «Альпы AGT». Завод — свойство коллекции, а не расцветки: в имени товара он повторяется
    у всех и не различает ничего.

    **Почему хвост, а не любое повторение.** Название расцветки внутри коллекции
    уникально — «Альфа», «Вега», «Гамма», — поэтому повторяющееся слово расцветкой быть не
    может. Но у многих марок первым словом идёт род: «Дуб Авила», «Дуб Прато». «Дуб» тоже
    повторяется, и он — часть названия. Разделяет их ПОЛОЖЕНИЕ: род стоит спереди, маркер
    сзади. Поэтому первое слово не трогаем никогда, а хвост снимаем.

    **Достаточно двух совпадений, а не всех.** У той же коллекции «Effect» пометка AGT
    стоит лишь у семи позиций из четырнадцати — поставщик проставил её не везде. Требуй мы
    совпадения у всех, самый очевидный случай и не сработал бы.
    """
    rows = [str(t or "").split() for t in titles]
    rows = [r for r in rows if len(r) > 1]
    if len(rows) < 2:
        return set()

    seen: dict[str, int] = {}
    for row in rows:
        # Первое слово — вне игры: там живёт род («Дуб»), а не маркер.
        for word in dict.fromkeys(row[1:]):
            key = word.casefold()
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


def plan(items, tm_code: str, tm_name: str, only_collection: str = ""):
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
            skipped.append((item, Skipped.DIRTY_TITLE))
            continue

        groups.setdefault((collection, product_type), []).append(item)

    inputs = []
    found_noise: set[str] = set()
    for (collection, product_type), members in groups.items():
        first = members[0]
        # Шум считается ПО КОЛЛЕКЦИИ целиком: одна позиция о повторе ничего не знает.
        noise = shared_tail((i.site_name or "").strip() for i in members)
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
                "title": drop_shared((i.site_name or "").strip(), noise),
                # Размер отдаём как есть: нужен он в имени или нет, решит
                # `plan_collection` по виду товара (`size_in_name`), а не мы.
                "tail": (i.size or "").strip(),
            } for i in members],
        })

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
           noise: set[str] | None = None) -> str:
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
