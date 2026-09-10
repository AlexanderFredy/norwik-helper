"""Правки справочника номенклатуры: сборка имён, разница с 1С, операции для `set-items`.

Отношение к `changes.py` ровно такое же, как у цен: здесь ЧИСТЫЕ ПРАВИЛА, а инструмент
агента (`pricing_tools.py`) только подаёт им данные и печатает результат. Причина та же,
что у `retail.py` и `naming.py`: правило, отданное модели, исполняется по-разному от вызова
к вызову, а расхождение в справочнике обнаруживается через месяц и не там, где возникло.

ГЛАВНОЕ РАЗДЕЛЕНИЕ ТРУДА (§19.5): **модель отдаёт ЧАСТИ, строку собирает код.**

Модель присылает вид товара, марку, коллекцию, название расцветки и артикул по отдельности
— имя из них складывает `build_name`. Иначе одна и та же коллекция получает то
«Виниловый ламинат Linderwood Quartz Адана LQ-01», то «Linderwood QUARTZ адана», и увидеть
это можно только глазами, по одной карточке.

ЧТО СЧИТАЕТСЯ ПРАВКОЙ, А ЧТО НОРМАЛИЗАЦИЕЙ (§19.8.1)

Смена регистра и лишние пробелы — НЕ правка: их делают пачками, они ничего не меняют по
смыслу, и админу про них говорят одной строкой «проведена нормализация» (§19.9). Всё
остальное — родитель, коллекция, размеры, коэффициент упаковки, артикул — правка: её
показывают поимённо и пишут в историю.

Различие держится на `naming.significant`: сравниваются схлопнутые по пробелам и регистру
формы, как в `set-items.bsl`. Две реализации одного правила — на стороне 1С и здесь — это
осознанно: 1С решает, что писать в историю, а мы решаем, что показывать админу ДО записи.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.price_tool import discontinued
from src.price_tool.naming import (collection_case, drop_own_article,
                                   ensure_type_prefix, tidy, violations)

# Поля, изменение которых показывается админу и менеджерам поимённо (§19.9).
WATCHED = ("parent_ref", "collection", "article", "unit", "pack_coefficient",
           "length_from", "length_to", "width_from", "width_to", "thickness")

TITLES = {
    "parent_ref": "папка",
    "collection": "коллекция",
    "article": "артикул",
    "unit": "единица",
    "pack_coefficient": "коэффициент упаковки",
    "length_from": "длина",
    "length_to": "длина до",
    "width_from": "ширина",
    "width_to": "ширина до",
    "thickness": "толщина",
    "name": "наименование",
    "full_name": "полное наименование",
    "site_name": "наименование для сайта",
}


def significant(before, after) -> bool:
    """Изменение по существу, а не нормализация регистра и пробелов."""
    if isinstance(before, str) or isinstance(after, str):
        return _form(before) != _form(after)
    return before != after


def _form(value) -> str:
    return " ".join(str(value or "").split()).lower()


def build_name(product_type: str, tm: str, collection: str, title: str,
               tail: str = "") -> str:
    """Наименование из частей: `[вид товара] [ТМ] [коллекция] [название] [размер]`.

    `tail` — РАЗМЕР, и только он: `60x60`, `1290x190x12`. Артикула в шаблоне нет (§19.5) —
    он лежит в отдельном реквизите, по которому идёт сопоставление с прайсом, и в имени
    только занимает место. Раньше этот параметр был описан как «артикул или размер», и
    агент честно ставил туда артикул: на боевой базе так вышло 53 позиции.

    Размер нужен там, где он РАЗЛИЧАЕТ позиции, — у керамики «Мадейра» это сразу несколько
    товаров с разными форматами. У ламината он одинаков на всю коллекцию и место ему в
    имени папки (§19.5), а не в каждой карточке. У дверей размер не пишется вовсе.
    Пустой хвост — нормальный случай, а не пропуск.

    Вид товара ставится ЧЕРЕЗ `ensure_type_prefix`, а не простой склейкой: он же чинит
    архаизмы («Водостойкий ламинат» → «Виниловый ламинат»), иначе старое имя не заменялось
    бы, а дополнялось.
    """
    coll = collection_case(collection)
    parts = [tm, coll, title, tail]
    body = " ".join(p for p in (str(x or "").strip() for x in parts) if p)
    return tidy(ensure_type_prefix(body, product_type))


def site_name(title: str, tail: str = "") -> str:
    """Наименование для сайта — только название расцветки (§19.5).

    Ни вида товара, ни марки, ни коллекции: они на сайте и так известны из карточки. Артикул
    сюда тоже не дописывается — на боевой базе он там оказался и был убран как отсебятина.
    """
    return tidy(" ".join(p for p in (str(title or "").strip(),
                                     str(tail or "").strip()) if p))


@dataclass(frozen=True)
class FieldChange:
    field: str
    before: object
    after: object

    @property
    def significant(self) -> bool:
        return significant(self.before, self.after)

    def render(self) -> str:
        title = TITLES.get(self.field, self.field)
        before = "(пусто)" if self.before in (None, "") else self.before
        after = "(пусто)" if self.after in (None, "") else self.after
        return f"{title}: {before} → {after}"


@dataclass(frozen=True)
class ItemPlan:
    """Одна позиция: что с ней делаем и почему."""
    op: str                       # create | update
    article: str
    name: str
    full_name: str
    site_name: str
    ref: str = ""                 # у create пусто — код присвоит 1С
    parent_ref: str = ""
    changes: list[FieldChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fields: dict = field(default_factory=dict)     # что уйдёт в set-items
    # ОТКАЗ — не то же самое, что «нечего менять». Позиция разобрана, правка нужна, но
    # отправить её нельзя: не хватает данных, и запись сделала бы хуже, чем бездействие.
    # Отдельный флаг нужен, потому что у создания «нечего менять» не бывает — оно всегда
    # попало бы в операции.
    blocked: bool = False

    @property
    def real_changes(self) -> list[FieldChange]:
        return [c for c in self.changes if c.significant]

    @property
    def only_normalization(self) -> bool:
        """Правка есть, но вся она — регистр и пробелы. Админу такое показывают одной
        строкой на коллекцию, менеджерам не показывают вовсе (§19.9)."""
        return self.op == "update" and bool(self.changes) and not self.real_changes


@dataclass(frozen=True)
class CollectionPlan:
    tm_code: str
    tm_name: str
    product_type: str             # код вида товара
    product_type_name: str
    collection: str
    items: list[ItemPlan] = field(default_factory=list)
    new_folder: dict | None = None      # {parent_ref, name} — папку коллекции ещё не завели
    warnings: list[str] = field(default_factory=list)

    FOLDER_ID = "collection"      # ключ $id для ссылки на папку внутри батча (§19.2.3)

    @property
    def created(self) -> list[ItemPlan]:
        return [i for i in self.items if i.op == "create" and not i.blocked]

    @property
    def updated(self) -> list[ItemPlan]:
        return [i for i in self.items
                if i.op == "update" and i.real_changes and not i.blocked]

    @property
    def normalized(self) -> list[ItemPlan]:
        return [i for i in self.items if i.only_normalization and not i.blocked]

    @property
    def touched(self) -> list[ItemPlan]:
        return self.created + self.updated + self.normalized

    def ops(self) -> list[dict]:
        """Операции для `set-items` в порядке, в котором их выполнит 1С.

        Папка коллекции идёт первой и получает `$id`: товары ссылаются на неё как
        `"$collection"`, потому что кода у неё ещё нет (§19.2.3). Провалится папка —
        товары придут со статусом `skipped_dependency`, а не создадутся в корне.
        """
        out: list[dict] = []

        if self.new_folder:
            out.append({"op": "create_folder", "id": self.FOLDER_ID,
                        "parent_ref": self.new_folder["parent_ref"],
                        "name": self.new_folder["name"]})

        for item in self.touched:
            if item.op == "create":
                op = {"op": "create_item", "name": item.name,
                      "full_name": item.full_name, "site_name": item.site_name,
                      "article": item.article, "product_type": self.product_type,
                      "manufacturer": self.tm_code,
                      "parent_ref": item.parent_ref or f"${self.FOLDER_ID}"}
                op.update(item.fields)
            else:
                op = {"op": "update_item", "ref": item.ref, "name": item.name,
                      "full_name": item.full_name, "site_name": item.site_name}
                op.update(item.fields)
                if item.parent_ref:
                    op["parent_ref"] = item.parent_ref
            out.append(op)

        return out


# --- разбор входа модели ---------------------------------------------------------------
#
# Модель присылает удобные ей поля (`length`, `width`), 1С принимает диапазоны
# (`length_from`/`length_to`). Разворачиваем здесь: у большинства товаров размер точный, и
# заставлять модель дублировать каждое число значит копить ошибки на ровном месте. Диапазон
# передаётся явными `*_from`/`*_to` — так у плитки, где длина «от 600 до 1200».


def _span(raw: dict, key: str) -> tuple[float | None, float | None]:
    exact = _num(raw.get(key))
    if exact is not None:
        return exact, exact
    return _num(raw.get(f"{key}_from")), _num(raw.get(f"{key}_to"))


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _props(raw) -> list[dict]:
    out = []
    for p in raw or []:
        code = str(p.get("property") or "").strip()
        value = str(p.get("value_code") or "").strip()
        if code and value:
            out.append({"property": code, "value_code": value})
    return out


def plan_collection(inp: dict, current: list, scope: list[str] | None = None
                    ) -> CollectionPlan:
    """Разбор предложения модели в план правок по ОДНОЙ коллекции.

    `current` — позиции марки из 1С (`NomItem`): по ним считается «было». Позиции, которой
    модель просит правку, в 1С может не оказаться — это не исключение, а замечание: код
    товара агент мог взять из устаревшей выгрузки, и молча создавать вместо правки нельзя.

    `scope` — категории из `/categories` (§19.6). Проверяется ЗДЕСЬ, а не моделью, ровно по
    той же причине, что и в `propose_prices`: это ограничение админа, и обходить его
    рассуждением нельзя.
    """
    by_ref = {i.ref: i for i in current}
    tm_name = str(inp.get("tm_name") or "").strip()
    type_name = str(inp.get("product_type_name") or "").strip()
    collection = collection_case(inp.get("collection"))
    warnings = [str(w) for w in (inp.get("warnings") or []) if str(w).strip()]

    if scope and type_name and not _in_scope(type_name, scope):
        warnings.append(
            f"⚠️ «{type_name}» вне категорий из /categories — правки не предлагаю")
        return CollectionPlan(tm_code=str(inp.get("tm_code") or ""), tm_name=tm_name,
                              product_type=str(inp.get("product_type") or ""),
                              product_type_name=type_name, collection=collection,
                              warnings=warnings)

    plans: list[ItemPlan] = []

    # СОСТАВ КОЛЛЕКЦИЙ В 1С и то, что план вообще трогает: нужно, чтобы не расщепить
    # коллекцию частичным переименованием (см. `_keep_collection` ниже).
    members: dict[str, set] = {}
    for existing in current:
        if not existing.not_exported:
            members.setdefault(_form(existing.collection), set()).add(existing.ref)
    plan_refs = {str(r.get("ref") or "").strip()
                 for r in inp.get("items") or [] if r.get("ref")}

    for raw in inp.get("items") or []:
        op = "update" if str(raw.get("op") or "").startswith("upd") else "create"
        ref = str(raw.get("ref") or "").strip()
        article = str(raw.get("article") or "").strip()
        title = str(raw.get("title") or "").strip()
        tail = str(raw.get("tail") or "").strip()

        was = by_ref.get(ref) if op == "update" else None

        item_warnings: list[str] = []
        # СВОЙ артикул вычищаем ПОСЛЕ сборки, а не полагаемся на то, что модель не
        # передаст его в `tail`: она уже передавала, и молча.
        name = drop_own_article(
            build_name(type_name, tm_name, collection, title, tail), article)
        full = drop_own_article(
            str(raw.get("full_name") or "").strip() or name, article)
        site = drop_own_article(site_name(title), article)

        for bad in violations(name):
            item_warnings.append(f"имя содержит {bad}")
        if op == "update" and was is None:
            item_warnings.append(
                f"позиции {ref or '(без кода)'} нет в выгрузке 1С — правку не отправляю")

        # НЕТ НАЗВАНИЯ РАСЦВЕТКИ — НАИМЕНОВАНИЯ НЕ ТРОГАЕМ.
        #
        # `build_name` собирает строку из частей, и без `title` она схлопывается до
        # «Ламинат Peli Vintage»: одинаковой для всей коллекции и без единого признака
        # позиции. План принял бы это за законное переименование и стёр расцветки у всех
        # пяти позиций разом. На прогоне 10.09.2026 модель так и сделала — заметила сама и
        # прислала предложение заново, но полагаться на это нельзя: правка молчаливая и
        # разрушительная, а «было» после записи взять уже неоткуда.
        #
        # Отказываем ТОЛЬКО в части имён: остальные поля позиции (свойства, размеры,
        # упаковка) от этого не портятся, и терять их из-за одного пропущенного поля незачем.
        if not title:
            if op == "create":
                item_warnings.append(
                    "не передано название расцветки (title) — позицию не создаю: "
                    "имя вышло бы одинаковым для всей коллекции")
                plans.append(ItemPlan(
                    op=op, ref=ref, article=article, name=name, full_name=full,
                    site_name=site, warnings=item_warnings, blocked=True))
                continue
            name = getattr(was, "name", "") or name
            full = getattr(was, "full_name", "") or full
            site = getattr(was, "site_name", "") or site
            item_warnings.append(
                "не передано название расцветки (title) — наименования оставил как есть")

        length_from, length_to = _span(raw, "length")
        width_from, width_to = _span(raw, "width")
        wanted = {
            "parent_ref": str(raw.get("parent_ref") or "").strip(),
            "article": article,
            "unit": str(raw.get("unit") or "").strip(),
            "pack_coefficient": _num(raw.get("pack_coefficient")),
            "length_from": length_from, "length_to": length_to,
            "width_from": width_from, "width_to": width_to,
            "thickness": _num(raw.get("thickness")),
        }

        target = _discontinued_target(wanted["parent_ref"],
                                      str(inp.get("product_type") or ""))
        if target:
            # ЖЁСТКИЙ МАППИНГ «вид товара → папка снятых» (§19.2.5). В боевой базе папок
            # снятых 23 штуки, разложены они исторически, и выбор «похожей» на глаз — это
            # ровно тот способ, которым туда попала нынешняя каша. Папку назначает КОД.
            if target != wanted["parent_ref"]:
                item_warnings.append(
                    f"перенос в снятые: папка исправлена на {target} по маппингу вида товара")
                wanted["parent_ref"] = target

        changes: list[FieldChange] = []
        fields: dict = {}

        if op == "create":
            fields = {k: v for k, v in wanted.items() if v not in (None, "")}
            fields.pop("parent_ref", None)          # уедет отдельным полем операции
        elif was is not None:
            # ИМЕНА СРАВНИВАЕМ ВСЕГДА, остальные поля — только переданные. Отсутствие поля
            # значит «не трогать» (§19.8): модель шлёт разницу, а не полную карточку, и
            # пустой `unit` не должен выглядеть как требование очистить единицу.
            for key, new in (("name", name), ("full_name", full), ("site_name", site)):
                old = getattr(was, key, "") or ""
                if _form(old) != _form(new) or old != new:
                    changes.append(FieldChange(key, old, new))
            for key, new in wanted.items():
                if new in (None, ""):
                    continue
                old = _current_value(was, key)
                if significant(old, new):
                    changes.append(FieldChange(key, old, new))
                    fields[key] = new
            if any(c.field in ("name", "full_name", "site_name") and c.significant
                   for c in changes):
                fields.update({"name": name, "full_name": full, "site_name": site})

        props = _props(raw.get("properties"))
        if props and op == "create":
            fields["properties"] = props
        elif props:
            known = {p.code: p.value_code for p in getattr(was, "properties", ())} if was else {}
            fresh = [p for p in props if known.get(p["property"]) != p["value_code"]]
            if fresh:
                fields["properties"] = fresh
                changes.append(FieldChange("свойства", len(known), len(known) + len(fresh)))

        plans.append(ItemPlan(
            op=op, ref=ref, article=article, name=name, full_name=full, site_name=site,
            parent_ref=wanted["parent_ref"], changes=changes,
            warnings=item_warnings, fields=fields))

    warnings += _name_split(type_name, tm_name, collection, current, plan_refs, plans)
    warnings += _unfilled_properties(collection, current)

    folder = inp.get("new_folder") or None
    if folder and folder.get("parent_ref"):
        folder = {"parent_ref": str(folder["parent_ref"]),
                  "name": collection_case(folder.get("name") or collection)}
    else:
        folder = None

    return CollectionPlan(
        tm_code=str(inp.get("tm_code") or ""), tm_name=tm_name,
        product_type=str(inp.get("product_type") or ""), product_type_name=type_name,
        collection=collection, items=plans, new_folder=folder, warnings=warnings)



def _name_split(type_name: str, tm_name: str, collection: str, current: list,
                plan_refs: set, plans: list) -> list[str]:
    """Останется ли коллекция единообразной после правки. Иначе — предупреждение.

    СЛУЧАЙ, ПОРОДИВШИЙ ПРОВЕРКУ (10.09.2026). У коллекции Anatolia Platinium свойство
    «Коллекция» в 1С равно `Platinium`, а наименования собраны как «Ламинат Peli **Anatolia**
    Platinium …» — лишнее слово попало в имена, но не в свойство. Агент чинил сломанное
    полное наименование ОДНОЙ позиции и собрал ей имя по правилам §19.5, то есть без
    «Anatolia». Он был прав: канон строится из свойства. Но одиннадцать соседей остались в
    прежнем виде, и коллекция, до того единообразная, разъехалась на два написания.

    Блокировать нельзя — правка была нужна, а запрет оставил бы сломанное имя как есть.
    Поэтому код не мешает, а НАЗЫВАЕТ последствие: сколько позиций останется в другом виде
    и какие. Дальше это видит и админ в предложении, и модель в ответе инструмента —
    следующим шагом коллекция приводится целиком.
    """
    touched = {p.ref for p in plans if p.ref and not p.blocked}
    if not touched:
        return []

    prefix = build_name(type_name, tm_name, collection, "").strip()
    if not prefix:
        return []

    stale = [i for i in current
             if _form(i.collection) == _form(collection)
             and not i.not_exported
             and i.ref not in touched and i.ref not in plan_refs
             and not _form(i.name).startswith(_form(prefix))]

    if not stale:
        return []

    shown = ", ".join(i.article or i.ref for i in stale[:6])
    return [f"⚠️ Наименования {len(stale)} поз. коллекции собраны иначе, чем правленые "
            f"({shown}{', …' if len(stale) > 6 else ''}). Коллекция останется в двух "
            f"написаниях. Если канон — «{prefix} …», передай в этом же вызове и остальные "
            "позиции; если прежний вид верен, значит расходится свойство «Коллекция»."]


def _unfilled_properties(collection: str, current: list) -> list[str]:
    """Свойства, пустые у ВСЕЙ коллекции, но заполненные у других коллекций марки.

    ЭТО ФАКТ ИЗ ДАННЫХ, А НЕ НАПОМИНАНИЕ В ПРОМПТЕ. Прайс LINDERWOOD указывает фаску
    `V-Groove` на весь раздел, и у Design с Platinium она в 1С стоит, а у Vintage, Loft и
    Grand пуста — 19 позиций. На одном прогоне агент это заметил и предложил заполнить, на
    следующем прошёл мимо: правило жило только в тексте промпта. Теперь пробел виден в
    самом ответе инструмента, каждый раз и без исключений.

    Сравниваем именно с другими коллекциями МАРКИ: набор свойств у вида товара широкий, и
    ругаться на всё незаполненное значило бы шуметь. А вот свойство, которое у соседних
    коллекций той же марки заполнено, у этой пустое не просто так.
    """
    wanted = _form(collection)
    mine = [i for i in current if _form(i.collection) == wanted and not i.not_exported]
    if not mine:
        return []

    filled_here = {p.property for i in mine for p in i.properties if p.value}
    filled_elsewhere: dict[str, int] = {}
    for item in current:
        if _form(item.collection) == wanted or item.not_exported:
            continue
        for prop in item.properties:
            if prop.value:
                filled_elsewhere[prop.property] = filled_elsewhere.get(prop.property, 0) + 1

    gaps = sorted(name for name in filled_elsewhere if name not in filled_here)
    if not gaps:
        return []

    return [f"ℹ️ Не заполнено ни у одной из {len(mine)} поз. коллекции: "
            + ", ".join(gaps)
            + ". У других коллекций этой марки заполнено — проверь прайс: если значение "
              "там есть, проставь его этим же вызовом."]

def _in_scope(product_type: str, scope: list[str]) -> bool:
    """Нестрогое сравнение, как в `scope.py`: «плитка» покрывает «Керамическую плитку»."""
    low = product_type.lower()
    return any(c.lower() in low or low in c.lower() for c in scope)


def _current_value(item, key: str):
    if key == "parent_ref":
        return getattr(item, "collection_ref", "")
    if key == "unit":
        return getattr(item, "unit", "")
    if key == "pack_coefficient":
        units = getattr(item, "alt_units", {}) or {}
        return next(iter(units.values()), None)
    return getattr(item, key, None)


# --- показ админу ----------------------------------------------------------------------


def render(plan: CollectionPlan) -> str:
    """Предложение по коллекции: коротко, поимённо только там, где это правка.

    ОДИНАКОВЫЕ ПРАВКИ ПО ВСЕЙ КОЛЛЕКЦИИ НЕ РАСПИСЫВАЮТСЯ ПОЗИЦИЯМИ (§19.9). На коллекции в
    сорок расцветок построчный список «коэффициент 1.8 → 2.23» сорок раз — это не отчёт, а
    стена, в которой теряется единственная настоящая проблема.
    """
    lines = [f"Коллекция {plan.collection} ({plan.tm_name})"]

    if plan.new_folder:
        lines.append(f"➕ Новая папка коллекции: {plan.new_folder['name']}")

    if plan.created:
        lines.append(f"➕ Новых позиций: {len(plan.created)}")
        for i in plan.created[:10]:
            lines.append(f"   {i.article}  {i.name}")
        if len(plan.created) > 10:
            lines.append(f"   … и ещё {len(plan.created) - 10}")

    if plan.updated:
        lines.append(f"✏️ Правок: {len(plan.updated)} поз.")
        for text, items in _grouped(plan.updated).items():
            if len(items) == len(plan.updated) and len(items) > 1:
                lines.append(f"   вся коллекция — {text}")
            elif len(items) > 3:
                lines.append(f"   {len(items)} поз. — {text}")
            else:
                lines.append(f"   {', '.join(i.article or i.name for i in items)} — {text}")

    if plan.normalized:
        lines.append(f"ℹ️ Нормализация (регистр, пробелы): {len(plan.normalized)} поз. — "
                     "в историю не пишется")

    for item in plan.items:
        for w in item.warnings:
            lines.append(f"⚠️ {item.article or item.ref or '(новая)'}: {w}")

    lines += list(plan.warnings)

    if not plan.touched and not plan.new_folder:
        lines.append("Расхождений нет — править нечего.")

    return "\n".join(lines)


def _grouped(items: list[ItemPlan]) -> dict:
    """Позиции по ОДИНАКОВОМУ набору правок: ключ — их текстовое описание."""
    out: dict[str, list[ItemPlan]] = {}
    for i in items:
        key = "; ".join(c.render() for c in i.real_changes)
        out.setdefault(key, []).append(i)
    return out


def _discontinued_target(parent_ref: str, product_type_ref: str) -> str | None:
    """Целевая папка снятых, если товар переносят именно туда. Иначе None.

    Опознаём перенос по тому, что папка-получатель ЕСТЬ В МАППИНГЕ: список папок снятых
    известен заранее, и определять их по имени («содержит СНЯТ») незачем — на именах уже
    один раз обожглись, когда коллекция «Стародуб … снят с производства» была принята за
    папку снятых.
    """
    if not parent_ref:
        return None
    known = {t.folder_ref for t in discontinued.MAPPING.values() if t.folder_ref}
    if parent_ref not in known:
        return None
    return discontinued.folder_for(product_type_ref) or parent_ref
