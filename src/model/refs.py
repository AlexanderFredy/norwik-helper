"""Адресация предметов задачи (§3.3 specs/agent-workflow-model.md).

**Почему у предмета НЕСКОЛЬКО идентификаторов, а не один.** LLM в пределах одной сессии
зовёт коллекцию то именем из прайса, то кодом папки из 1С. По одному идентификатору один и
тот же предмет выглядел бы как два разных, и правило «повторное создание задачи — дополнение
существующей» превратилось бы в «заведи вторую».

Приём и его обоснование — из `_collection_keys` (`src/agent/pricing_tools.py`):
**лишний ключ безвреден, недостающий запер бы работу намертво.**

Товар адресуется кодом 1С, если он там есть; если нет — артикулом (если есть) и
наименованием из прайса. У ещё не созданного товара кода не существует в принципе.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.model.enums import TaskSubject
from src.price_tool.scope import normalize


def _clean(value: str | None) -> str:
    return (value or "").strip()


def norm_article(value: str | None) -> str:
    """Артикул без разделителей вовсе: «LE-263», «LE 263» и «le263» — один артикул.

    Общая `scope.normalize` для этого не годится: она заменяет пунктуацию ПРОБЕЛОМ, и
    «LE-263» превращается в «le 263», которое не совпадёт с «le263». Поставщики пишут
    артикул как придётся, и различать эти написания значило бы заводить дубли на пустом
    месте.
    """
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


@dataclass(frozen=True)
class Ref:
    """Идентификаторы одного предмета: марки, коллекции или товара.

    Все поля необязательны — у предмета может не быть ни кода, ни артикула. Пустой `Ref` не
    совпадает ни с чем, включая другой пустой: «неизвестно» не равно «неизвестно».
    """
    code: str = ""                          # код 1С (марки, папки коллекции, товара)
    article: str = ""                       # артикул поставщика
    names: tuple[str, ...] = ()             # имена КАК НАПИСАНЫ; сравнение — через `keys`

    @classmethod
    def make(cls, code: str | None = None, article: str | None = None,
             names=()) -> "Ref":
        """Собрать `Ref`. Имена хранятся КАК НАПИСАНЫ, сравниваются нормализованными.

        Раньше здесь оседала нормализованная форма, и она же попадала админу на глаза:
        задача подписывалась «most flooring / ле паркет» вместо «Most Flooring / Ле
        Паркет». Нормализация нужна для СРАВНЕНИЯ, а не для показа, — и путать эти две
        роли не стоит: читает подпись человек.

        Повторы отбрасываются по нормализованной форме: «Ле Паркет» и «ЛЕ ПАРКЕТ» — одно
        имя, и держать оба незачем.
        """
        if isinstance(names, str):
            names = [names]
        seen: list[str] = []
        keys: set[str] = set()
        for raw in names:
            value = _clean(raw)
            key = normalize(value)
            if key and key not in keys:
                keys.add(key)
                seen.append(value)
        return cls(code=_clean(code), article=norm_article(article),
                   names=tuple(seen))

    @property
    def keys(self) -> set[str]:
        """Имена в нормализованной форме — то, по чему идёт сравнение."""
        return {normalize(name) for name in self.names}

    @property
    def empty(self) -> bool:
        return not (self.code or self.article or self.names)

    def matches(self, other: "Ref") -> bool:
        """Совпадение хотя бы по одному НЕПУСТОМУ идентификатору.

        Именно «хотя бы по одному», а не по всем: у старой задачи может быть только имя, у
        новой — уже и код папки, появившийся после создания коллекции в 1С.
        """
        if self.empty or other.empty:
            return False
        if self.code and other.code and self.code == other.code:
            return True
        if self.article and other.article and self.article == other.article:
            return True
        return bool(self.keys & other.keys)

    def label(self) -> str:
        """Как называть предмет админу: сперва имя, иначе артикул, иначе код."""
        if self.names:
            return self.names[0]
        return self.article or self.code or "без имени"

    def merged(self, other: "Ref") -> "Ref":
        """Объединить known-о-себе двух ссылок на один предмет.

        Нужно, когда задача встретилась повторно и принесла идентификатор, которого раньше
        не было: код папки появился после её создания. Теряя его, мы потеряли бы совпадение
        в следующий раз.
        """
        # Сравнение при слиянии — тоже по нормализованной форме: иначе «Ле Паркет» и
        # «ЛЕ ПАРКЕТ» осели бы двумя именами одного предмета.
        mine = self.keys
        names = list(self.names) + [n for n in other.names if normalize(n) not in mine]
        return Ref(code=self.code or other.code,
                   article=self.article or other.article,
                   names=tuple(names))


@dataclass(frozen=True)
class TaskAddress:
    """Куда направлена задача: марка плюс коллекция либо марка плюс товар."""
    tm: Ref
    subject: Ref
    subject_kind: TaskSubject = TaskSubject.COLLECTION

    def matches(self, other: "TaskAddress") -> bool:
        """Та же марка И тот же предмет того же рода.

        Марка обязательна в сравнении: артикул «A001» может встретиться у двух поставщиков,
        и без марки задачи разных брендов слились бы в одну.
        """
        return (self.subject_kind == other.subject_kind
                and self.tm.matches(other.tm)
                and self.subject.matches(other.subject))

    def merged(self, other: "TaskAddress") -> "TaskAddress":
        return TaskAddress(tm=self.tm.merged(other.tm),
                           subject=self.subject.merged(other.subject),
                           subject_kind=self.subject_kind)

    def label(self) -> str:
        # У задачи на марку целиком предмет И ЕСТЬ марка: «Egger / Egger» — бессмыслица,
        # которую админ прочтёт как ошибку.
        if self.subject_kind == TaskSubject.MARK:
            return self.tm.label()
        return f"{self.tm.label()} / {self.subject.label()}"


@dataclass(frozen=True)
class TradeMark:
    """ТМ, найденная в прайсе (§3.1).

    `in_1c=False` — марки в 1С нет либо LLM не смогла её сопоставить. Такая ТМ всё равно
    попадает в список: «не нашли» — это факт о прайсе, который админу нужно видеть, а не
    повод молча выбросить раздел.
    """
    name: str
    code: str = ""
    in_1c: bool = True

    @classmethod
    def unknown(cls, name: str) -> "TradeMark":
        return cls(name=name, code="", in_1c=False)

    def as_ref(self) -> Ref:
        return Ref.make(code=self.code, names=[self.name])
