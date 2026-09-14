"""Модель работы с прайсами — синглтон, применяющий команды (§1, §5–§7).

Список прайсов живёт в памяти процесса и пишется в базу при каждом изменении. Агент один,
визуалы к базе напрямую не обращаются — поэтому синглтон в памяти остаётся настоящим.

**Выполнение задачи здесь ЗАГЛУШКА:** в 1С ничего не пишется. Пока проверяется обвязка —
список прайсов, задачи, статусы, захват. Настоящее выполнение придёт вместе с инструментами
LLM (§6.1–6.2), и подменить надо будет ровно один метод `_execute`.
"""
from __future__ import annotations

import logging

from src.model import locks as lk
from src.model import price_list as pl
from src.model.commands import Command, CommandKind, Rejected
from src.model.enums import PriceStatus, TaskStatus
from src.model.events import Broadcaster, Event, EventKind
from src.model.price import Price
from src.model.task import PriceTask

logger = logging.getLogger(__name__)


class PriceListService:
    def __init__(self, model_store, supplier_store, save_file, broadcaster=None,
                 build_tasks=None) -> None:
        self._store = model_store
        self._suppliers = supplier_store
        self._save_file = save_file
        # Формирование задач агентом (§6.1). None — падаем на заглушку: так тесты идут
        # без сети и без ключа, а бот без настроенного 1С всё равно показывает список.
        self._build_tasks = build_tasks
        self.events = broadcaster or Broadcaster()
        self._prices: list[Price] = []
        self._locks: dict[int, lk.Lock] = {}

    # ------------------------------------------------------------------ старт

    async def load(self) -> None:
        self._prices = await self._store.load_all()
        self._locks = await self._store.load_locks()
        pl.relink(self._prices)
        await self._store.save_links(self._prices)

    # ------------------------------------------------------------------ чтение

    @property
    def prices(self) -> list[Price]:
        return list(self._prices)

    def price(self, price_id: int) -> Price | None:
        return next((p for p in self._prices if p.id == price_id), None)

    def task(self, task_id: int) -> tuple[Price, PriceTask] | tuple[None, None]:
        for price in self._prices:
            found = price.task_by_id(task_id)
            if found is not None:
                return price, found
        return None, None

    def lock_of(self, price_id: int) -> lk.Lock | None:
        return self._locks.get(price_id)

    def busy_for(self, actor: str) -> set[int]:
        return lk.busy_for(self._locks, actor)

    # ------------------------------------------------------------------ приём

    async def submit(self, content: bytes, filename: str, *, supplier_hint: str = "",
                     force: bool = False, received_at: str | None = None,
                     actor: str = "") -> str:
        from src.model.intake import submit as do_submit

        result = await do_submit(content, filename, suppliers=self._suppliers,
                                 model_store=self._store, prices=self._prices,
                                 supplier_hint=supplier_hint, force=force,
                                 received_at=received_at, save_file=self._save_file)

        if result.price is not None:
            await self._fill_tasks(result.price, content, filename)

        if result.price is None:
            await self.events.publish(Event(
                EventKind.PRICE_REJECTED, actor=actor,
                text=f"Прайс «{filename}» не принят: {result.reason}."
                     + (" Принудительно добавить: /model_force с тем же файлом."
                        if result.outdated else "")))
            return result.reason

        self._prices.append(result.price)
        pl.relink(self._prices)
        await self._store.save_links(self._prices)

        tail = " Помечен как устаревший." if result.price.has_newer else ""
        await self.events.publish(Event(
            EventKind.PRICE_ADDED, price_id=result.price.id, actor=actor,
            text=f"Прайс «{filename}» принят (№{result.price.id}, поставщик "
                 f"«{result.supplier_name}»), задач: {len(result.price.tasks)}.{tail}"))
        return ""

    # -------------------------------------------------------------- применение

    async def apply(self, command: Command) -> None:
        """Применить одну команду. Исключение не должно ронять цикл."""
        try:
            await self._apply(command)
        except Exception:                               # noqa: BLE001
            logger.exception("Команда %s не применилась", command.label())
            await self.events.publish(Event(
                EventKind.COMMAND_REJECTED, actor=command.actor,
                text=f"Не удалось выполнить: {command.label()}. Подробности в логах."))

    async def _apply(self, command: Command) -> None:
        kind = command.kind

        if kind == CommandKind.EXECUTE_TASK:
            return await self._execute(command)
        if kind == CommandKind.SET_TASK_STATUS:
            return await self._set_task_status(command)
        if kind == CommandKind.EDIT_TASK_DESCRIPTION:
            return await self._edit_description(command)
        if kind == CommandKind.DELETE_TASK:
            return await self._delete_task(command)
        if kind == CommandKind.SET_PRICE_STATUS:
            return await self._set_price_status(command)
        if kind == CommandKind.REBUILD_TASKS:
            return await self._rebuild(command)
        if kind == CommandKind.DESTROY_PRICE:
            return await self._destroy(command)
        if kind == CommandKind.RELEASE_LOCK:
            return await self._release(command)

        logger.warning("Неизвестная команда: %s", kind)

    # ------------------------------------------------------------------ задачи

    async def _execute(self, command: Command) -> None:
        """ЗАГЛУШКА выполнения: в 1С ничего не пишется.

        Здесь встанет вызов LLM с идентификатором, актуальным описанием, прошлым результатом
        и текущим статусом (§6.2). Пока отмечаем задачу выполненной с явной пометкой, чтобы
        результат нельзя было принять за настоящую запись.
        """
        price, task = self.task(command.task_id)
        if task is None:
            await self._reject(command, "задача не найдена — возможно, список пересобрали")
            return

        # Правка описания могла приехать вместе с командой: при замещении в очереди
        # полезная нагрузка объединяется (§7), и свежий текст лежит здесь же.
        text = (command.payload or {}).get("text")
        if text:
            task.set_description(text)

        if task.status == TaskStatus.DONE:
            await self._reject(command, "задача уже выполнена, перечитайте состояние")
            return

        await self._take_lock(price, command.actor)
        await self.events.publish(Event(
            EventKind.TASK_RUNNING, price_id=price.id, task_id=task.id,
            text=f"Задача {task.id} взята в работу: {task.label()}"))

        task.complete(TaskStatus.DONE,
                      "ЗАГЛУШКА: выполнение не производилось, в 1С ничего не записано")
        await self._store.update_task(task)
        await self._after_task(price)

        await self.events.publish(Event(
            EventKind.TASK_STATUS, price_id=price.id, task_id=task.id,
            text=f"Задача {task.id} — {task.status.value}. {task.result}"))

    async def _set_task_status(self, command: Command) -> None:
        price, task = self.task(command.task_id)
        if task is None:
            return await self._reject(command, "задача не найдена")
        raw = (command.payload or {}).get("status", "")
        try:
            status = TaskStatus(raw)
        except ValueError:
            return await self._reject(command, f"неизвестный статус «{raw}»")

        task.complete(status, task.result) if status.closed else task.reopen()
        await self._store.update_task(task)
        await self.events.publish(Event(
            EventKind.TASK_STATUS, price_id=price.id, task_id=task.id,
            text=f"Задача {task.id} — {status.value}."))

    async def _edit_description(self, command: Command) -> None:
        price, task = self.task(command.task_id)
        if task is None:
            return await self._reject(command, "задача не найдена")
        task.set_description((command.payload or {}).get("text", ""))
        await self._store.update_task(task)
        await self.events.publish(Event(
            EventKind.TASK_STATUS, price_id=price.id, task_id=task.id,
            text=f"Описание задачи {task.id} изменено."))

    async def _delete_task(self, command: Command) -> None:
        price, task = self.task(command.task_id)
        if task is None:
            return await self._reject(command, "задача не найдена")
        price.remove_task(task.id)
        await self._store.remove_task(task.id)
        await self.events.publish(Event(
            EventKind.TASK_REMOVED, price_id=price.id, task_id=task.id,
            text=f"Задача {task.id} удалена."))

    # ------------------------------------------------------------------ прайсы

    async def _set_price_status(self, command: Command) -> None:
        price = self.price(command.price_id)
        if price is None:
            return await self._reject(command, "прайс не найден")
        raw = (command.payload or {}).get("status", "")
        try:
            status = PriceStatus(raw)
        except ValueError:
            return await self._reject(command, f"неизвестный статус «{raw}»")

        price.status = status
        await self._store.set_price_status(price.id, status)
        await self.events.publish(Event(
            EventKind.PRICE_STATUS, price_id=price.id,
            text=f"Прайс №{price.id} — {status.value}."))

    async def _rebuild(self, command: Command) -> None:
        price = self.price(command.price_id)
        if price is None:
            return await self._reject(command, "прайс не найден")

        # Пересборка при выполняющейся задаче отклоняется: прерывание могло бы застать
        # запись в 1С на середине (§6.3). Заглушка выполняется мгновенно, но правило
        # закладывается сейчас, чтобы не всплыло при настоящем выполнении.
        lock = self._locks.get(price.id)
        if lock is not None and lock.working and not lock.expired():
            return await self._reject(command, "по прайсу идёт задача, дождитесь её конца")

        from src.storage import price_files

        content = price_files.load(price.supplier_price.file_path)
        if content is None:
            return await self._reject(command, "файл прайса не найден на сервере")

        fresh = await self._make_tasks(price, content, price.supplier_price.filename)
        price.rebuild(fresh)
        await self._store.replace_tasks(price.id, price.tasks)

        await self.events.publish(Event(
            EventKind.TASKS_REBUILT, price_id=price.id,
            text=f"Задачи прайса №{price.id} собраны заново: {len(price.tasks)}. "
                 "Статусы и правки описаний не переносятся."))

    async def _make_tasks(self, price: Price, content: bytes,
                          filename: str) -> list[PriceTask]:
        """Список задач: агентом, если он есть, иначе заглушкой.

        Пустой ответ агента — не повод оставить прайс без задач: падаем на заглушку и
        говорим об этом. Молчаливо пустой список админ прочтёт как «разбирать нечего».
        """
        from src.model.intake import read_signature, stub_tasks

        supplier = await self._suppliers.get_supplier(price.supplier_price.supplier_id)
        name = supplier.name if supplier else "поставщик"

        if self._build_tasks is not None:
            try:
                tasks = await self._build_tasks(content, filename, price)
            except Exception:                           # noqa: BLE001
                logger.exception("Агент не составил задачи по %s", filename)
                tasks = []
            if tasks:
                return tasks
            await self.events.publish(Event(
                EventKind.TASKS_REBUILT, price_id=price.id,
                text="Агент не смог составить задачи — подставил заглушку. "
                     "Повторить: /rebuild " + str(price.id)))

        _, sheets = read_signature(content, filename)
        return stub_tasks(sheets, name)

    async def _fill_tasks(self, price: Price, content: bytes, filename: str) -> None:
        """Задачи при приёме. Прайс уже записан — дописываем список отдельно."""
        tasks = await self._make_tasks(price, content, filename)
        price.rebuild(tasks)
        await self._store.replace_tasks(price.id, price.tasks)

    async def _destroy(self, command: Command) -> None:
        price = self.price(command.price_id)
        if price is None:
            return await self._reject(command, "прайс не найден")

        await self._store.remove_price(price.id)
        self._prices = [p for p in self._prices if p.id != price.id]
        self._locks.pop(price.id, None)
        pl.relink(self._prices)
        await self._store.save_links(self._prices)

        await self.events.publish(Event(
            EventKind.PRICE_REMOVED, price_id=price.id,
            text=f"Прайс №{price.id} уничтожен вместе с задачами."))

    async def _release(self, command: Command) -> None:
        lock = self._locks.get(command.price_id)
        if lock is None:
            return await self._reject(command, "прайс не захвачен")
        if lock.actor != command.actor:
            return await self._reject(command, "захват принадлежит другому администратору")
        await self._drop_lock(command.price_id, "снят досрочно")

    # ------------------------------------------------------------------ захват

    async def _take_lock(self, price: Price, actor: str) -> lk.Lock:
        previous = self._locks.get(price.id)
        if previous is None:
            number = await self._store.last_generation(price.id)
            previous = lk.Lock(price_id=price.id, actor="", generation=number,
                               acquired_at="", expires_at=lk.utcnow().isoformat())
        lock = lk.acquire(previous, price.id, actor)
        self._locks[price.id] = lock
        await self._store.save_lock(lock)
        await self.events.publish(Event(
            EventKind.LOCK_TAKEN, price_id=price.id,
            text=f"Прайс №{price.id} занят ({actor})."))
        return lock

    async def _after_task(self, price: Price) -> None:
        """Задача закончена: захват держим ещё минуту (§5.1)."""
        lock = self._locks.get(price.id)
        if lock is None:
            return
        waiting = lk.start_grace(lock)
        self._locks[price.id] = waiting
        await self._store.save_lock(waiting)

    async def expire_locks(self) -> list[int]:
        """Снять истёкшие захваты. Зовётся циклом на каждом обороте."""
        released = []
        for lock in lk.expired_locks(self._locks):
            await self._drop_lock(lock.price_id, "аренда истекла")
            released.append(lock.price_id)
        return released

    async def _drop_lock(self, price_id: int, why: str) -> None:
        self._locks.pop(price_id, None)
        await self._store.clear_lock(price_id)
        await self.events.publish(Event(
            EventKind.LOCK_RELEASED, price_id=price_id,
            text=f"Прайс №{price_id} свободен ({why})."))

    # ------------------------------------------------------------------ отказы

    async def _reject(self, command: Command, reason: str) -> None:
        await self.events.publish(Event(
            EventKind.COMMAND_REJECTED, price_id=command.price_id,
            task_id=command.task_id, actor=command.actor,
            text=f"{command.label()}: {reason}."))

    async def reject(self, rejected: Rejected) -> None:
        """Отказ, вынесенный разбором пачки (`plan_batch`)."""
        await self._reject(rejected.command, rejected.reason)
