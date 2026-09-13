"""Показ справочников поставщиков админу (§2.3 spec/agent-workflow-model.md).

Чистый модуль: принимает готовые данные, отдаёт текст. Ни запросов, ни Telegram — так его
можно покрыть тестами целиком, а формат проверить, не поднимая бота.

Списки сортируются по дате добавления (это делает хранилище) и показывают её в формате
`дд.мм.гг чч.мм` — так требует спека.
"""
from __future__ import annotations

from datetime import datetime


def human_dt(raw: str | None) -> str:
    """ISO-время → `дд.мм.гг чч.мм`.

    В базе время хранится в UTC, показываем в ЛОКАЛЬНОЙ зоне процесса: админ читает список
    глазами, и «14.32» должно совпадать с тем, что было на его часах. Если зона сервера
    отличается от зоны админа, лечится настройкой сервера, а не форматом.
    """
    if not raw:
        return ""
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()
    return stamp.strftime("%d.%m.%y %H.%M")


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def render_suppliers(suppliers, counts: dict[int, tuple[int, int]] | None = None) -> str:
    """Список поставщиков: номер, имя, когда заведён, сколько сигнатур и файлов.

    Номер — порядковый по списку, а не `id`: админ набирает его в командах, и «3» короче и
    понятнее, чем «17». Соответствие номера и id держит вызывающий.
    """
    if not suppliers:
        return ("Справочник поставщиков пуст. Он заполнится сам, когда пришлёте прайс: "
                "поставщика можно назвать в подписи к файлу.")

    counts = counts or {}
    lines = [f"Поставщики ({len(suppliers)}):", ""]
    for i, s in enumerate(suppliers, 1):
        sig, files = counts.get(s.id, (0, 0))
        tail = (f" — {sig} {_plural(sig, 'сигнатура', 'сигнатуры', 'сигнатур')}, "
                f"{files} {_plural(files, 'файл', 'файла', 'файлов')}") if counts else ""
        lines.append(f"{i}. {s.name}{tail}")
        lines.append(f"   заведён {human_dt(s.created_at)}")
    return "\n".join(lines)


def signature_label(sig) -> str:
    """Чем эта сигнатура отличается от соседних — назначение, иначе имя файла."""
    return sig.purpose or sig.sample_name or f"формат {sig.signature[:12]}"


def render_signatures(rows, owner_names: dict[int, str] | None = None,
                      files: dict[int, int] | None = None) -> str:
    """Сигнатуры, сгруппированные по поставщику.

    `rows` — пары (номер, сигнатура), и номер приходит СНАРУЖИ, а не считается здесь:
    он сквозной по всему справочнику. Если бы нумерация зависела от того, отфильтрован
    список или нет, `/signature_delete 2` удалял бы разное в разных видах.
    """
    if not rows:
        return "Сигнатур нет."

    owner_names = owner_names or {}
    files = files or {}
    lines = [f"Сигнатуры ({len(rows)}):"]
    current = None
    for num, sig in rows:
        owner = owner_names.get(sig.supplier_id, f"поставщик #{sig.supplier_id}")
        if owner != current:
            lines.append("")
            lines.append(f"— {owner} —")
            current = owner
        n = files.get(sig.id, 0)
        lines.append(f"{num}. {signature_label(sig)} — "
                     f"{n} {_plural(n, 'файл', 'файла', 'файлов')}")
        lines.append(f"   впервые {human_dt(sig.first_seen)}, "
                     f"последний раз {human_dt(sig.last_seen)}")
    return "\n".join(lines)


def render_files(rows, labels: dict[int, str] | None = None) -> str:
    """Файлы прайсов, сгруппированные по сигнатуре. Номера сквозные, как у сигнатур."""
    if not rows:
        return "Файлов прайсов нет."

    labels = labels or {}
    lines = [f"Файлы прайсов ({len(rows)}):"]
    current = None
    for num, f in rows:
        group = labels.get(f.signature_id, f"сигнатура #{f.signature_id}")
        if group != current:
            lines.append("")
            lines.append(f"— {group} —")
            current = group
        lines.append(f"{num}. {f.filename}")
        got = human_dt(f.received_at)
        lines.append(f"   добавлен {human_dt(f.added_at)}"
                     + (f", получен {got}" if got else ""))
    return "\n".join(lines)


def render_merge(result, source_name: str, target_name: str) -> str:
    """Итог слияния: что именно переехало.

    Показываем числа, а не «готово»: слияние необратимо, и админ должен увидеть, сходится
    ли результат с тем, что он ожидал.
    """
    parts = []
    if result.moved:
        parts.append(f"перенесено сигнатур: {result.moved}")
    if result.absorbed:
        parts.append(f"слилось с уже имевшимися: {result.absorbed}")
    if result.files:
        parts.append(f"файлов прайсов: {result.files}")
    body = "; ".join(parts) if parts else "переносить было нечего"

    tail = (f"Поставщик «{source_name}» уничтожен." if result.removed
            else f"Поставщик «{source_name}» остался — удалить не удалось.")
    return f"«{source_name}» влит в «{target_name}»: {body}.\n{tail}"
