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


@dataclass(frozen=True)
class Ref:
    """Идентификаторы одного предмета: марки, коллекции или товара.

    Все поля необязательны — у предмета может не быть ни кода, ни артикула. Пустой `Ref` не
    совпадает ни с чем, включая другой пустой: «неизвестно» не равно «неизвестно».
    """
    code: str = ""                          # код 1С (марки, папки коллекции, товара)
    article: str = ""                       # артикул поставщика
    names: tuple[str, ...] = ()             # нормализованные имена, все известные

    @classmethod
    def make(cls, code: str | None = None, article: str | None = None,
             names=()) -> "Ref":
        """Собрать `Ref`, нормализовав имена и отбросив пустые."""
        if isinstance(names, str):
            names = [names]
        seen: list[str] = []
        for raw in names:
            norm = normalize(_clean(raw))
            if norm and norm not in seen:
                seen.append(norm)
        return cls(code=_clean(code), article=normalize(_clean(article)),
                   names=tuple(seen))

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
        return bool(set(self.names) & set(other.names))

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
        names = list(self.names) + [n for n in other.names if n not in self.names]
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
