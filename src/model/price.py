"""Прайс в списке обработки (§3.1–3.2 specs/agent-workflow-model.md)."""
from __future__ import annotations

from dataclasses import dataclass, field

from src.model.enums import PriceStatus, TaskKind
from src.model.refs import TradeMark
from src.model.task import PriceTask, now, sort_tasks


@dataclass
class SupplierPrice:
    """Представление прайса поставщика: чей он, что в нём и где лежит файл.

    **Без файла объект существовать не может** — это инвариант, а не пожелание: прайс без
    файла нечего разбирать, а модель перестала бы быть источником правды о том, что лежит
    на сервере.

    Список ТМ пуст при создании и заполняется ПОСЛЕ разбора: файл приходит раньше, чем
    становится известно его содержимое.

    `received_at` — когда прайс пришёл агенту. `price_date` — дата ВНУТРИ файла, если её
    удалось прочитать; она может отсутствовать (у Most Floor её в шапке нет вовсе), и
    именно поэтому свежесть считается по двум датам, а не по одной.
    """
    supplier_id: int
    file_id: int
    file_path: str
    filename: str = ""
    received_at: str | None = None
    price_date: str | None = None
    signature: str = ""
    trade_marks: list[TradeMark] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.file_path:
            raise ValueError("прайс без ссылки на файл существовать не может")

    def freshness(self) -> dict:
        """Ключ для `freshness.is_newer`: сперва дата прайса, потом дата получения."""
        return {"price_date": self.price_date, "received_at": self.received_at}

    def set_trade_marks(self, marks: list[TradeMark]) -> None:
        self.trade_marks = list(marks)

    @property
    def unknown_marks(self) -> list[TradeMark]:
        """ТМ, которых в 1С нет. Факт о прайсе, который админу нужно видеть."""
        return [m for m in self.trade_marks if not m.in_1c]


@dataclass
class Price:
    """Элемент списка обработки.

    Статус СТАВИТ АДМИН (§3.2). Модель считает только готовность: если бы статус выводился
    автоматически, одна задача с исходом «частично обработана» — а это штатный исход —
    запирала бы прайс в «частично обработан» навсегда.
    """
    supplier_price: SupplierPrice
    id: int | None = None
    status: PriceStatus = PriceStatus.TODO
    created_at: str = field(default_factory=now)
    newer_id: int | None = None
    tasks: list[PriceTask] = field(default_factory=list)

    # --------------------------------------------------------- более свежий

    @property
    def has_newer(self) -> bool:
        """Признак — производная от ссылки, отдельным полем не хранится."""
        return self.newer_id is not None

    def clear_newer(self) -> None:
        """Снять пометку: более новый прайс привязали ошибочно."""
        self.newer_id = None

    # ---------------------------------------------------------------- задачи

    @property
    def ready(self) -> bool:
        """Готов к закрытию: все задачи закрыты. Подсказка визуалу, не переход.

        Пустой список задач готовым НЕ считается: «разбирать нечего» и «всё сделано» —
        разные вещи, и admin должен увидеть первое как вопрос, а не как результат.
        """
        return bool(self.tasks) and all(t.closed for t in self.tasks)

    @property
    def sorted_tasks(self) -> list[PriceTask]:
        return sort_tasks(self.tasks)

    def find_task(self, task: PriceTask) -> PriceTask | None:
        """Задача того же вида по тому же адресу, если она уже есть."""
        return next((t for t in self.tasks if t.matches(task)), None)

    def add_task(self, task: PriceTask) -> PriceTask:
        """Завести задачу либо дополнить существующую.

        Пара (адрес, вид) уникальна в пределах прайса: повторное создание — не вторая
        задача, а дополнение описания первой (§3.3). Возвращается та задача, которая в
        итоге живёт в списке, — вызывающему она нужна, чтобы сослаться на неё.
        """
        existing = self.find_task(task)
        if existing is not None:
            existing.absorb(task)
            return existing
        self.tasks.append(task)
        return task

    def remove_task(self, task_id: int) -> bool:
        """Уничтожить задачу. Право только у админа — модель это не проверяет,
        проверяет вызывающий."""
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t.id != task_id]
        return len(self.tasks) < before

    def task_by_id(self, task_id: int) -> PriceTask | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def rebuild(self, tasks: list[PriceTask]) -> None:
        """Пересобрать список задач с нуля (§6.3).

        **Ничего не переносится** — ни статусы, ни правки описаний. Список строится из
        сегодняшнего состояния 1С, и уже сделанная работа в него не попадёт сама собой:
        расхождений по ней больше нет.

        Задача, от которой админ отказался, вернётся — это принятое следствие, а не
        недосмотр: «собери заново» и означает «начать с чистого листа». Не чинить.
        """
        self.tasks = []
        for task in tasks:
            self.add_task(task)

    # ---------------------------------------------------------------- прочее

    def counts(self) -> dict[TaskKind, int]:
        out: dict[TaskKind, int] = {}
        for task in self.tasks:
            out[task.kind] = out.get(task.kind, 0) + 1
        return out

    def label(self) -> str:
        return self.supplier_price.filename or self.supplier_price.file_path
