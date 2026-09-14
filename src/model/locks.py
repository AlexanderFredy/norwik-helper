"""Захват прайса (§5 specs/agent-workflow-model.md).

Захватывается **прайс целиком**, а не отдельная задача: задачи одного прайса конфликтуют
между собой — правка справочника и цены по одной коллекции.

Чистые правила с ЯВНО передаваемым временем: иначе тест на истечение аренды пришлось бы
писать через `sleep`, а он бы то проходил, то нет.

Три слоя защиты от того, чтобы прогон продолжал работать после снятия захвата:

1. **Аренда с продлением.** Выполняющийся цикл продлевает её на каждой итерации, и
   «10 минут» означает *10 минут без признаков жизни*, а не 10 минут от старта. Живая
   тяжёлая задача захват не теряет — теряет только зависшая.
2. **Отмена прогона** при истечении аренды. Единственный слой, который реально прекращает
   работу и расход токенов; живёт в цикле агента, не здесь.
3. **Маркер поколения** (`generation`). Отмена может прийти между ответом API и записью,
   поэтому каждому захвату выдаётся возрастающий номер, и `valid_for` отвергает работу
   устаревшего прогона. **Проверка обязана стоять непосредственно перед записью в 1С**, а
   не только перед сменой статуса: необратима именно запись.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

#: Аренда: столько захват живёт БЕЗ ПРИЗНАКОВ ЖИЗНИ, а не от старта.
LEASE_SECONDS = 600          # 10 минут

#: После выполнения задачи захват держится ещё столько. Тот же админ скорее всего
#: продолжит следующей задачей по этому прайсу, и за это время визуалы успевают обновиться.
GRACE_SECONDS = 60           # 1 минута


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(raw: str) -> datetime:
    stamp = datetime.fromisoformat(raw)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Lock:
    """Захват одного прайса.

    `generation` растёт при каждом новом захвате и НЕ сбрасывается: маркер поколения должен
    быть строго возрастающим, иначе прогон из прошлой жизни совпал бы с нынешним номером.
    """
    price_id: int
    actor: str
    generation: int
    acquired_at: str
    expires_at: str
    working: bool = True          # False — идёт минута ожидания после выполнения задачи

    def expired(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= _parse(self.expires_at)

    def held_by(self, actor: str, now: datetime | None = None) -> bool:
        return self.actor == actor and not self.expired(now)

    def seconds_left(self, now: datetime | None = None) -> int:
        left = (_parse(self.expires_at) - (now or utcnow())).total_seconds()
        return max(0, int(left))


def acquire(previous: Lock | None, price_id: int, actor: str,
            now: datetime | None = None) -> Lock:
    """Взять захват. Вызывающий обязан сперва убедиться, что прайс свободен (`free_for`).

    Номер поколения продолжает прежний, а не начинается заново: устаревший прогон не должен
    случайно совпасть с новым.
    """
    now = now or utcnow()
    generation = (previous.generation + 1) if previous else 1
    return Lock(price_id=price_id, actor=actor, generation=generation,
                acquired_at=now.isoformat(),
                expires_at=(now + timedelta(seconds=LEASE_SECONDS)).isoformat())


def free_for(lock: Lock | None, actor: str, now: datetime | None = None) -> bool:
    """Может ли этот админ работать с прайсом.

    Свободен, если захвата нет, он истёк или принадлежит этому же админу. Отнять чужой
    живой захват нельзя — второй админ ждёт (§5.1).
    """
    if lock is None:
        return True
    if lock.expired(now):
        return True
    return lock.actor == actor


def renew(lock: Lock, now: datetime | None = None) -> Lock:
    """Продлить аренду — зовётся на каждой итерации работающего прогона."""
    now = now or utcnow()
    return replace(lock, expires_at=(now + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                   working=True)


def start_grace(lock: Lock, now: datetime | None = None) -> Lock:
    """Задача выполнена: захват не снимается сразу, держим минуту (§5.1)."""
    now = now or utcnow()
    return replace(lock, expires_at=(now + timedelta(seconds=GRACE_SECONDS)).isoformat(),
                   working=False)


def valid_for(lock: Lock | None, generation: int, actor: str,
              now: datetime | None = None) -> bool:
    """Маркер поколения: имеет ли прогон право менять состояние и писать в 1С.

    Проверяется НЕПОСРЕДСТВЕННО ПЕРЕД ЗАПИСЬЮ в 1С, а не только перед сменой статуса
    задачи: между отменой прогона и записью есть промежуток, и необратима именно запись.
    """
    if lock is None:
        return False
    return (lock.generation == generation
            and lock.actor == actor
            and not lock.expired(now))


def expired_locks(locks: dict[int, Lock], now: datetime | None = None) -> list[Lock]:
    """Захваты, у которых аренда истекла: их снимают, а прогоны отменяют."""
    now = now or utcnow()
    return [lock for lock in locks.values() if lock.expired(now)]


def busy_for(locks: dict[int, Lock], actor: str,
             now: datetime | None = None) -> set[int]:
    """Прайсы, закрытые для ЭТОГО админа: захвачены другим и захват ещё жив."""
    return {pid for pid, lock in locks.items() if not free_for(lock, actor, now)}
