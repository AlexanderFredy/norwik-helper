"""Очередь команд визуалов (§7, §9 specs/agent-workflow-model.md).

Два правила, которые ломаются тихо и потому проверяются подробно: схлопывание присваиваний
(в модель должно приехать последнее решение админа, а не цепочка промежуточных) и «кто
первый» по времени создания НА СТОРОНЕ ВИЗУАЛА, а не по порядку обхода провайдеров.
"""
import tempfile
import unittest
from pathlib import Path

from src.model.commands import Command, CommandKind, order_batch, plan_batch
from src.storage.command_queue import CommandQueue


def cmd(kind=CommandKind.EXECUTE_TASK, price=1, task=None, at="2026-09-13T10:00:00",
        source="telegram", actor="admin-1", **payload):
    return Command(kind=kind, price_id=price, task_id=task, created_at=at,
                   source=source, actor=actor, payload=payload)


class PlanTest(unittest.TestCase):
    """Чистое правило разбора пачки — без базы."""

    def test_order_is_by_visual_time_not_arrival(self):
        late = cmd(at="2026-09-13T10:00:05")
        early = cmd(at="2026-09-13T10:00:01")
        late.id, early.id = 1, 2          # в очередь легла раньше поздняя
        self.assertEqual([c.id for c in order_batch([late, early])], [2, 1])

    def test_ties_are_broken_deterministically(self):
        a, b = cmd(at="2026-09-13T10:00:00"), cmd(at="2026-09-13T10:00:00")
        a.id, b.id = 7, 3
        self.assertEqual([c.id for c in order_batch([a, b])], [3, 7])

    def test_first_wins_among_exclusive_commands(self):
        first = cmd(task=1, at="2026-09-13T10:00:01")
        second = cmd(task=2, at="2026-09-13T10:00:02")
        run, rejected = plan_batch([second, first])
        self.assertEqual(run, [first])
        self.assertEqual(len(rejected), 1)
        self.assertIn("занят", rejected[0].reason)

    def test_different_prices_do_not_block_each_other(self):
        a = cmd(price=1, at="2026-09-13T10:00:01")
        b = cmd(price=2, at="2026-09-13T10:00:02")
        run, rejected = plan_batch([a, b])
        self.assertEqual(len(run), 2)
        self.assertEqual(rejected, [])

    def test_assignments_are_not_subject_to_first_wins(self):
        """Смена статуса мгновенна: отвечать «занято» на вторую подряд — ломать работу."""
        a = cmd(kind=CommandKind.SET_TASK_STATUS, task=1, at="2026-09-13T10:00:01")
        b = cmd(kind=CommandKind.SET_TASK_STATUS, task=2, at="2026-09-13T10:00:02")
        run, rejected = plan_batch([a, b])
        self.assertEqual(len(run), 2)
        self.assertEqual(rejected, [])

    def test_assignment_passes_even_when_price_is_busy(self):
        a = cmd(kind=CommandKind.EDIT_TASK_DESCRIPTION, task=1)
        run, rejected = plan_batch([a], busy_prices={1})
        self.assertEqual(run, [a])

    def test_already_locked_price_rejects_work_commands(self):
        a = cmd(price=1)
        run, rejected = plan_batch([a], busy_prices={1})
        self.assertEqual(run, [])
        self.assertIn("другим администратором", rejected[0].reason)

    def test_command_without_price_is_not_blocked(self):
        """«Принять прайс» ещё не относится ни к какому прайсу."""
        a = Command(kind=CommandKind.SUBMIT_PRICE, created_at="2026-09-13T10:00:00")
        run, _ = plan_batch([a], busy_prices={1, 2})
        self.assertEqual(run, [a])

    def test_rebuild_and_execute_compete_for_the_same_price(self):
        run_first = cmd(kind=CommandKind.EXECUTE_TASK, task=1, at="2026-09-13T10:00:01")
        rebuild = cmd(kind=CommandKind.REBUILD_TASKS, at="2026-09-13T10:00:02")
        run, rejected = plan_batch([rebuild, run_first])
        self.assertEqual(run, [run_first])
        self.assertEqual(rejected[0].command.kind, CommandKind.REBUILD_TASKS)


class QueueTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.q = CommandQueue(Path(self._dir.name) / "t.db")
        await self.q.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_put_and_pending(self):
        await self.q.put(cmd(task=5))
        pending = await self.q.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].task_id, 5)
        self.assertEqual(pending[0].kind, CommandKind.EXECUTE_TASK)

    async def test_payload_survives(self):
        await self.q.put(cmd(kind=CommandKind.SET_TASK_STATUS, task=5, status="выполнена"))
        self.assertEqual((await self.q.pending())[0].payload["status"], "выполнена")

    async def test_pending_is_ordered_by_visual_time(self):
        await self.q.put(cmd(task=1, at="2026-09-13T10:00:09"))
        await self.q.put(cmd(task=2, at="2026-09-13T10:00:01"))
        self.assertEqual([c.task_id for c in await self.q.pending()], [2, 1])

    async def test_status_change_overwrites_instead_of_queueing_twice(self):
        """В модель приезжает последнее решение, а не цепочка промежуточных."""
        await self.q.put(cmd(kind=CommandKind.SET_TASK_STATUS, task=5, status="выполнена"))
        await self.q.put(cmd(kind=CommandKind.SET_TASK_STATUS, task=5,
                             at="2026-09-13T10:00:30", status="к обработке"))

        pending = await self.q.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].payload["status"], "к обработке")
        self.assertEqual(pending[0].created_at, "2026-09-13T10:00:30")

    async def test_status_of_another_task_is_a_separate_command(self):
        await self.q.put(cmd(kind=CommandKind.SET_TASK_STATUS, task=5, status="выполнена"))
        await self.q.put(cmd(kind=CommandKind.SET_TASK_STATUS, task=6, status="выполнена"))
        self.assertEqual(len(await self.q.pending()), 2)

    async def test_price_status_coalesces_by_price(self):
        await self.q.put(cmd(kind=CommandKind.SET_PRICE_STATUS, price=1, status="выполнен"))
        await self.q.put(cmd(kind=CommandKind.SET_PRICE_STATUS, price=1,
                             status="частично обработан"))
        pending = await self.q.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].payload["status"], "частично обработан")

    async def test_description_edits_coalesce(self):
        await self.q.put(cmd(kind=CommandKind.EDIT_TASK_DESCRIPTION, task=5, text="раз"))
        await self.q.put(cmd(kind=CommandKind.EDIT_TASK_DESCRIPTION, task=5, text="два"))
        pending = await self.q.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].payload["text"], "два")

    async def test_execute_commands_do_not_coalesce(self):
        """Каждая — отдельное намерение, и ответить надо на каждую."""
        await self.q.put(cmd(task=5))
        await self.q.put(cmd(task=5, at="2026-09-13T10:00:30"))
        self.assertEqual(len(await self.q.pending()), 2)

    async def test_take_marks_and_hides(self):
        await self.q.put(cmd(task=1))
        taken = await self.q.take()
        self.assertEqual(len(taken), 1)
        self.assertEqual(await self.q.pending(), [])
        self.assertEqual(len(await self.q.taken()), 1)

    async def test_done_removes(self):
        await self.q.put(cmd(task=1))
        taken = await self.q.take()
        self.assertTrue(await self.q.done(taken[0].id))
        self.assertEqual(await self.q.taken(), [])

    async def test_release_puts_it_back(self):
        await self.q.put(cmd(task=1))
        taken = await self.q.take()
        await self.q.release(taken[0].id)
        self.assertEqual(len(await self.q.pending()), 1)

    async def test_taken_command_does_not_coalesce_with_a_new_one(self):
        """Взятая уже в работе: перезаписав её, мы подменили бы то, что сейчас исполняется."""
        await self.q.put(cmd(kind=CommandKind.SET_TASK_STATUS, task=5, status="выполнена"))
        await self.q.take()
        await self.q.put(cmd(kind=CommandKind.SET_TASK_STATUS, task=5, status="к обработке"))
        self.assertEqual(len(await self.q.pending()), 1)
        self.assertEqual(len(await self.q.taken()), 1)

    async def test_requeue_stale_at_startup(self):
        """Взятая без завершения означает одно: процесс умер, не доработав."""
        await self.q.put(cmd(task=1))
        await self.q.put(cmd(task=2, at="2026-09-13T10:00:30"))
        await self.q.take()

        returned = await self.q.requeue_stale()

        self.assertEqual(returned, 2)
        self.assertEqual(len(await self.q.pending()), 2)
        self.assertEqual(await self.q.taken(), [])

    async def test_queue_survives_reopen(self):
        """Команда, присланная перед остановкой процесса, должна дождаться подъёма."""
        await self.q.put(cmd(task=7, actor="admin-2", source="1c"))
        again = CommandQueue(self.q._db_path)
        pending = await again.pending()
        self.assertEqual(pending[0].task_id, 7)
        self.assertEqual(pending[0].actor, "admin-2")
        self.assertEqual(pending[0].source, "1c")

    async def test_take_limit(self):
        for i in range(5):
            await self.q.put(cmd(task=i, at=f"2026-09-13T10:00:0{i}"))
        self.assertEqual(len(await self.q.take(limit=2)), 2)
        self.assertEqual(len(await self.q.pending()), 3)


class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    """Очередь плюс разбор пачки: так это будет работать в цикле агента."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.q = CommandQueue(Path(self._dir.name) / "t.db")
        await self.q.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_two_admins_on_one_price_first_wins(self):
        await self.q.put(cmd(task=1, actor="admin-1", at="2026-09-13T10:00:02",
                             source="telegram"))
        await self.q.put(cmd(task=2, actor="admin-2", at="2026-09-13T10:00:01",
                             source="1c"))

        run, rejected = plan_batch(await self.q.take())

        # Выиграл тот, кто НАЖАЛ раньше, а не тот, чей провайдер опросили первым.
        self.assertEqual(run[0].actor, "admin-2")
        self.assertEqual(rejected[0].command.actor, "admin-1")


if __name__ == "__main__":
    unittest.main()
