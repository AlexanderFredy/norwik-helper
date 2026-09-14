"""Захват прайса и маркер поколения (§5 specs/agent-workflow-model.md).

Время передаётся явно: иначе тест на истечение аренды пришлось бы писать через `sleep`,
и он то проходил бы, то нет.

Главное, что проверяется, — три слоя защиты от зомби-прогона. Снятие захвата по таймеру
работу НЕ останавливает, и без маркера поколения отменённый прогон дописал бы своё в 1С.
"""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from src.model import locks
from src.model.commands import Command, CommandKind, plan_batch
from src.model.locks import Lock, acquire, free_for, renew, start_grace, valid_for
from src.model.price import Price, SupplierPrice
from src.storage.model_store import ModelStore

T0 = locks.utcnow()


def later(seconds: int):
    return T0 + timedelta(seconds=seconds)


def price(path="/p/1.xlsx", file_id=1, signature="sig"):
    return Price(supplier_price=SupplierPrice(
        supplier_id=1, file_id=file_id, file_path=path, filename="п.xlsx",
        signature=signature, received_at="2026-09-01T10:00"))


class AcquireTest(unittest.TestCase):

    def test_free_price_can_be_taken(self):
        self.assertTrue(free_for(None, "admin-1", T0))

    def test_taken_price_is_closed_for_others(self):
        lock = acquire(None, 1, "admin-1", T0)
        self.assertFalse(free_for(lock, "admin-2", T0))

    def test_holder_is_not_blocked_by_his_own_lock(self):
        """Второй админ ждёт, но сам держатель работать обязан."""
        lock = acquire(None, 1, "admin-1", T0)
        self.assertTrue(free_for(lock, "admin-1", T0))

    def test_expired_lock_frees_the_price(self):
        lock = acquire(None, 1, "admin-1", T0)
        self.assertFalse(free_for(lock, "admin-2", later(locks.LEASE_SECONDS - 1)))
        self.assertTrue(free_for(lock, "admin-2", later(locks.LEASE_SECONDS + 1)))

    def test_generation_grows_and_never_restarts(self):
        """Иначе прогон из прошлой жизни совпал бы с номером нынешнего."""
        first = acquire(None, 1, "admin-1", T0)
        second = acquire(first, 1, "admin-2", later(1000))
        self.assertEqual((first.generation, second.generation), (1, 2))


class LeaseTest(unittest.TestCase):
    """Аренда считается от ПРИЗНАКОВ ЖИЗНИ, а не от старта."""

    def test_renew_extends_from_now(self):
        lock = acquire(None, 1, "admin-1", T0)
        alive = renew(lock, later(500))
        self.assertFalse(alive.expired(later(locks.LEASE_SECONDS + 10)))

    def test_heavy_but_alive_task_keeps_the_lock(self):
        """Полчаса работы с продлением каждые пять минут захват не теряют."""
        lock = acquire(None, 1, "admin-1", T0)
        for minute in range(5, 35, 5):
            lock = renew(lock, later(minute * 60))
        self.assertFalse(lock.expired(later(31 * 60)))

    def test_hung_task_loses_the_lock(self):
        lock = acquire(None, 1, "admin-1", T0)
        self.assertTrue(lock.expired(later(locks.LEASE_SECONDS + 1)))

    def test_grace_holds_a_minute_after_the_task(self):
        """Тот же админ скорее всего продолжит, и визуалы успеют обновиться."""
        lock = start_grace(acquire(None, 1, "admin-1", T0), later(100))
        self.assertFalse(lock.working)
        self.assertFalse(lock.expired(later(100 + locks.GRACE_SECONDS - 1)))
        self.assertTrue(lock.expired(later(100 + locks.GRACE_SECONDS + 1)))

    def test_grace_still_blocks_other_admins(self):
        lock = start_grace(acquire(None, 1, "admin-1", T0), later(100))
        self.assertFalse(free_for(lock, "admin-2", later(110)))

    def test_seconds_left(self):
        lock = acquire(None, 1, "admin-1", T0)
        self.assertEqual(lock.seconds_left(later(60)), locks.LEASE_SECONDS - 60)
        self.assertEqual(lock.seconds_left(later(99999)), 0)


class GenerationTest(unittest.TestCase):
    """Маркер поколения: проверка стоит НЕПОСРЕДСТВЕННО ПЕРЕД ЗАПИСЬЮ в 1С."""

    def test_own_running_generation_may_write(self):
        lock = acquire(None, 1, "admin-1", T0)
        self.assertTrue(valid_for(lock, lock.generation, "admin-1", later(10)))

    def test_cancelled_generation_may_not_write(self):
        """Тот самый зомби: аренда истекла, захват перехватили, а прогон ещё дописывает."""
        old = acquire(None, 1, "admin-1", T0)
        new = acquire(old, 1, "admin-2", later(locks.LEASE_SECONDS + 5))
        self.assertFalse(valid_for(new, old.generation, "admin-1",
                                   later(locks.LEASE_SECONDS + 10)))

    def test_expired_lock_invalidates_even_own_generation(self):
        lock = acquire(None, 1, "admin-1", T0)
        self.assertFalse(valid_for(lock, lock.generation, "admin-1",
                                   later(locks.LEASE_SECONDS + 1)))

    def test_another_actor_with_the_same_number_may_not_write(self):
        lock = acquire(None, 1, "admin-1", T0)
        self.assertFalse(valid_for(lock, lock.generation, "admin-2", later(10)))

    def test_released_lock_invalidates_everything(self):
        self.assertFalse(valid_for(None, 1, "admin-1", T0))


class BusyTest(unittest.TestCase):

    def test_busy_set_depends_on_who_asks(self):
        book = {1: acquire(None, 1, "admin-1", T0),
                2: acquire(None, 2, "admin-2", T0)}
        self.assertEqual(locks.busy_for(book, "admin-1", T0), {2})
        self.assertEqual(locks.busy_for(book, "admin-2", T0), {1})

    def test_expired_locks_are_listed_for_cleanup(self):
        book = {1: acquire(None, 1, "admin-1", T0),
                2: acquire(None, 2, "admin-2", later(locks.LEASE_SECONDS))}
        stale = locks.expired_locks(book, later(locks.LEASE_SECONDS + 1))
        self.assertEqual([lock.price_id for lock in stale], [1])


class BatchWithLocksTest(unittest.TestCase):
    """Правило «кто первый» плюс захват — так это работает на входе цикла."""

    def make(self, kind, price_id, at, actor="admin-1"):
        return Command(kind=kind, price_id=price_id, created_at=at, actor=actor)

    def test_rule_now_applies_to_every_command(self):
        """Решение админа от 14.09.2026: «кто первый» — для ВСЕХ команд."""
        first = self.make(CommandKind.SET_TASK_STATUS, 1, "2026-09-14T10:00:01")
        second = self.make(CommandKind.EDIT_TASK_DESCRIPTION, 1, "2026-09-14T10:00:02")
        run, rejected = plan_batch([second, first])
        self.assertEqual(run, [first])
        self.assertEqual(len(rejected), 1)

    def test_lock_of_another_admin_blocks_the_batch(self):
        book = {1: acquire(None, 1, "admin-2", T0)}
        mine = self.make(CommandKind.EXECUTE_TASK, 1, "2026-09-14T10:00:01", "admin-1")
        run, rejected = plan_batch([mine], locks.busy_for(book, "admin-1", T0))
        self.assertEqual(run, [])
        self.assertIn("другим администратором", rejected[0].reason)

    def test_own_lock_does_not_block_me(self):
        book = {1: acquire(None, 1, "admin-1", T0)}
        mine = self.make(CommandKind.EXECUTE_TASK, 1, "2026-09-14T10:00:01", "admin-1")
        run, _ = plan_batch([mine], locks.busy_for(book, "admin-1", T0))
        self.assertEqual(run, [mine])


class LockStorageTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = ModelStore(Path(self._dir.name) / "t.db")
        await self.store.init()
        self.price = await self.store.add_price(price())

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_lock_survives_reload(self):
        lock = acquire(None, self.price.id, "admin-1", T0)
        await self.store.save_lock(lock)

        loaded = await self.store.load_locks()

        self.assertEqual(loaded[self.price.id].actor, "admin-1")
        self.assertEqual(loaded[self.price.id].generation, 1)
        self.assertTrue(loaded[self.price.id].working)

    async def test_grace_flag_survives(self):
        lock = start_grace(acquire(None, self.price.id, "admin-1", T0), T0)
        await self.store.save_lock(lock)
        self.assertFalse((await self.store.load_locks())[self.price.id].working)

    async def test_cleared_lock_disappears_but_generation_stays(self):
        """Обнулив номер, следующий захват совпал бы с отменённым прогоном."""
        lock = acquire(None, self.price.id, "admin-1", T0)
        await self.store.save_lock(lock)

        self.assertTrue(await self.store.clear_lock(self.price.id))

        self.assertEqual(await self.store.load_locks(), {})
        self.assertEqual(await self.store.last_generation(self.price.id), 1)

    async def test_next_acquire_continues_numbering(self):
        first = acquire(None, self.price.id, "admin-1", T0)
        await self.store.save_lock(first)
        await self.store.clear_lock(self.price.id)

        previous = Lock(price_id=self.price.id, actor="", generation=
                        await self.store.last_generation(self.price.id),
                        acquired_at=T0.isoformat(), expires_at=T0.isoformat())
        second = acquire(previous, self.price.id, "admin-2", later(100))

        self.assertEqual(second.generation, 2)

    async def test_clearing_a_free_price_changes_nothing(self):
        self.assertFalse(await self.store.clear_lock(self.price.id))

    async def test_expired_lock_is_loaded_not_hidden(self):
        """Снятие сопровождается уведомлением визуалов — решает не хранилище."""
        lock = acquire(None, self.price.id, "admin-1", T0)
        await self.store.save_lock(lock)
        loaded = await self.store.load_locks()
        self.assertIn(self.price.id, loaded)
        self.assertTrue(loaded[self.price.id].expired(later(locks.LEASE_SECONDS + 1)))


if __name__ == "__main__":
    unittest.main()
