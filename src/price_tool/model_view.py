"""Показ списка прайсов и задач админу (§8 модели).

Чистый модуль: принимает объекты модели, отдаёт текст. Ни запросов, ни Telegram — формат
можно проверить, не поднимая бота.

Номера прайсов и задач — это их `id`, а не позиции в списке. Позиция сдвинулась бы при
уничтожении соседа, а команда «выполни задачу 7» обязана означать одно и то же всегда.
"""
from __future__ import annotations

from src.model.enums import TaskStatus
from src.price_tool.catalog_view import human_dt

_MARK = {TaskStatus.TODO: "•", TaskStatus.DONE: "✓", TaskStatus.PARTIAL: "~"}


def render_prices(prices, locks=None, suppliers=None) -> str:
    """Список прайсов: номер, файл, поставщик, статус, задачи, захват."""
    if not prices:
        return ("Список прайсов пуст. Пришлите файл прайса — он попадёт в модель.\n"
                "Устаревший принимается только принудительно: /model_force с файлом.")

    locks = locks or {}
    suppliers = suppliers or {}
    lines = [f"Прайсы ({len(prices)}):", ""]
    for price in prices:
        sp = price.supplier_price
        who = suppliers.get(sp.supplier_id, f"поставщик #{sp.supplier_id}")
        done = sum(1 for t in price.tasks if t.closed)

        head = f"№{price.id} · {sp.filename or sp.file_path} · {who}"
        if price.has_newer:
            head += f"  ⚠ устарел (свежее — №{price.newer_id})"
        lines.append(head)

        state = f"   {price.status.value} · задач {done}/{len(price.tasks)}"
        if price.ready:
            state += " · готов к закрытию"
        lines.append(state)

        lock = locks.get(price.id)
        if lock is not None:
            what = "в работе" if lock.working else "ждёт продолжения"
            lines.append(f"   🔒 занят: {lock.actor}, {what}, "
                         f"{lock.seconds_left()} с до снятия")

        lines.append(f"   принят {human_dt(price.created_at)}")
    lines.append("")
    lines.append("Задачи прайса: /tasks <номер>")
    return "\n".join(lines)


def render_tasks(price, supplier_name: str = "", lock=None) -> str:
    """Задачи одного прайса, в порядке видов (§3.3)."""
    sp = price.supplier_price
    head = [f"Прайс №{price.id} · {sp.filename or sp.file_path}"]
    if supplier_name:
        head.append(f"Поставщик: {supplier_name}")
    if sp.trade_marks:
        marks = ", ".join(m.name + ("" if m.in_1c else " (нет в 1С)")
                          for m in sp.trade_marks)
        head.append(f"ТМ в прайсе: {marks}")
    head.append(f"Статус: {price.status.value}"
                + (" · готов к закрытию" if price.ready else ""))
    if price.has_newer:
        head.append(f"⚠ Устарел: есть более свежий — №{price.newer_id}")
    if lock is not None:
        head.append(f"🔒 Занят: {lock.actor}, {lock.seconds_left()} с до снятия")

    if not price.tasks:
        head.append("")
        head.append("Задач нет. Собрать заново: /rebuild " + str(price.id))
        return "\n".join(head)

    head.append("")
    head.append(f"Задачи ({len(price.tasks)}), порядок — рекомендуемый:")
    head.append("")

    current = None
    for task in price.sorted_tasks:
        if task.kind != current:
            head.append(f"— {task.kind.value} —")
            current = task.kind
        head.append(f"{_MARK.get(task.status, '•')} {task.id}. "
                    f"{task.address.subject.label()}")
        if task.description:
            head.append(f"     {_short(task.description)}")
        if task.result:
            head.append(f"     итог: {_short(task.result)}")

    head.append("")
    head.append("Выполнить: /run <номер задачи> · описание: /edit <номер> <текст>")
    head.append("Статус: /status <номер> <к обработке|выполнена|частично обработана>")
    return "\n".join(head)


def _short(text: str, limit: int = 90) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"
