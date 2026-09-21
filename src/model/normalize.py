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


def collection_of(item) -> str:
    """Имя коллекции ДЛЯ НАИМЕНОВАНИЯ ТОВАРА, а не для показа.

    Свойство «Коллекция» заполнено — берём его. Пустое (у A+ Floor такими оказались все
    двадцать позиций) — остаётся имя папки, и вот тут ловушка: у восьми напольных
    категорий в имени папки СТОИТ РАЗМЕР по §19.5. Подставив его как коллекцию, мы
    получаем «Ламинат A+ Floor Ле Паркет 600x600x14 Авила» — размер в середине
    наименования, где его быть не должно: в имя товара он идёт только у керамики.

    Поэтому хвост-размер снимается точной обратной операцией к `folder_name`.
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
    for (collection, product_type), members in groups.items():
        first = members[0]
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
                "title": (i.site_name or "").strip(),
                # Размер отдаём как есть: нужен он в имени или нет, решит
                # `plan_collection` по виду товара (`size_in_name`), а не мы.
                "tail": (i.size or "").strip(),
            } for i in members],
        })

    return inputs, skipped, discontinued


def report(written: int, skipped: list, discontinued: int, groups: int) -> str:
    """Короткий отчёт админу: сделанное одной строкой, разбирательства — списком."""
    lines = []
    if written:
        lines.append(f"Наименования приведены к шаблону: {written} поз. "
                     f"в {groups} колл.")
    else:
        lines.append("Расхождений с шаблоном не нашлось — менять было нечего.")

    if discontinued:
        lines.append(f"Снятые с производства не трогал: {discontinued} поз.")

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
