"""Задача по прайсу (§3.3 specs/agent-workflow-model.md)."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from src.model.enums import TaskKind, TaskStatus, TaskSubject
from src.model.refs import TaskAddress


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PriceTask:
    """Одна задача.

    Изменяемый объект намеренно: у задачи есть жизненный цикл (статус, результат, дата
    выполнения), и притворяться, что это значение, значило бы городить копии на каждый шаг.

    `id` присваивает хранилище; до записи он `None`.
    """
    kind: TaskKind
    address: TaskAddress
    description: str = ""
    subject: TaskSubject = TaskSubject.COLLECTION
    status: TaskStatus = TaskStatus.TODO
    result: str = ""
    id: int | None = None
    created_at: str = field(default_factory=now)
    # КОГДА ЗАДАЧУ ПОСЛЕДНИЙ РАЗ ЗАПУСКАЛИ — независимо от исхода. В 1С это колонка
    # «Выполнена» (решение админа 21.09.2026: отдельного поля под прогон не заводим).
    #
    # Раньше отметка ставилась ТОЛЬКО при закрытии и снималась при возврате в очередь, и
    # тогда по ней нельзя было отличить «никто не брался» от «пробовали трижды и не вышло»,
    # а админу видеть это надо: задача в «к обработке» со вчерашним прогоном и задача,
    # которую не трогали, требуют разного. Ничего при этом не потеряно: у ЗАКРЫТОЙ задачи
    # закрывший её прогон — тот же самый, а историю прежних закрытий поле и не хранило.
    run_at: str | None = None

    def __post_init__(self) -> None:
        # Род предмета живёт и в адресе (для сравнения), и на задаче (для показа). Держим
        # их согласованными здесь, иначе рассинхронизация всплывёт при сопоставлении.
        self.subject = self.address.subject_kind

    # ------------------------------------------------------------- переходы

    def complete(self, status: TaskStatus, result: str = "") -> None:
        """Отметить исход. Ставит LLM после того, как САМА проверила запись в 1С.

        `TODO` тоже допустим: «не получилось ничего» — это возврат в очередь, а не отдельный
        статус. Отметка о прогоне при этом всё равно ставится: прогон был, чем бы он ни
        кончился, и именно это отличает «не получилось» от «не брались».
        """
        self.status = status
        self.result = result
        self.run_at = now()

    def reopen(self) -> None:
        """Вернуть в обработку: сбрасывается статус — и только он.

        Отметка о прогоне не снимается: задача снова не сделана, но попытка была, и
        скрывать её значит выдавать тронутую задачу за нетронутую.
        """
        self.status = TaskStatus.TODO

    def set_description(self, text: str) -> None:
        """Правка админа. Живёт только до исполнения — хранить её дольше не требуется:
        она нужна, чтобы подправить задание агенту прямо перед отправкой (§3.3)."""
        self.description = (text or "").strip()

    def absorb(self, other: "PriceTask") -> None:
        """Принять повторно заведённую задачу того же вида по тому же адресу.

        Не вторая задача, а ДОПОЛНЕНИЕ существующей (§3.3). Заодно подбираем
        идентификаторы, которых раньше не знали: код папки мог появиться после её создания,
        и без него следующее сопоставление промахнётся.
        """
        self.address = self.address.merged(other.address)
        extra = (other.description or "").strip()
        if extra and extra not in self.description:
            # ПУСТАЯ СТРОКА МЕЖДУ, а не перенос: описание пишется абзацами, и одиночный
            # перенос приклеил бы новый абзац к последней строке прежнего.
            self.description = f"{self.description}\n\n{extra}".strip()

    # -------------------------------------------------------------- прочее

    @property
    def closed(self) -> bool:
        return self.status.closed

    def matches(self, other: "PriceTask") -> bool:
        """Тот же вид И тот же адрес — то есть это одна и та же задача."""
        return self.kind == other.kind and self.address.matches(other.address)

    def label(self) -> str:
        return f"{self.kind.value}: {self.address.label()}"


def sort_tasks(tasks: list[PriceTask]) -> list[PriceTask]:
    """По видам в порядке из §3.3, внутри вида — по времени создания.

    Порядок ПОКАЗЫВАЕТСЯ, но не навязывается: выполнять админ может в любом.
    """
    return sorted(tasks, key=lambda t: (t.kind.order, t.created_at, t.id or 0))
