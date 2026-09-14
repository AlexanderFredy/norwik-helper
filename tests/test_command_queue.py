"""Очередь команд визуалов (§7, §9 specs/agent-workflow-model.md).

Два правила, которые ломаются тихо и потому проверяются подробно: схлопывание присваиваний
(в модель должно приехать последнее решение админа, а не цепочка промежуточных) и «кто
первый» по времени создания НА СТОРОНЕ ВИЗУАЛА, а не по порядку обхода провайдеров.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.model.commands import (Command, CommandKind, order_batch, plan_batch,
                                sort_time)
from src.storage.command_queue import CommandQueue


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def at_s(seconds: int = 0) -> str:
    """Метка рядом с «сейчас»: дальше часа срабатывает потолок доверия (`sort_time`)."""
    return (NOW + timedelta(seconds=seconds)).isoformat()


def cmd(kind=CommandKind.EXECUTE_TASK, price=1, task=None, at=None,
        source="telegram", actor="admin-1", **payload):
    return Command(kind=kind, price_id=price, task_id=task,
                   created_at=at or NOW.isoformat(),
                   source=source, actor=actor, payload=payload)


class PlanTest(unittest.TestCase):
    """Чистое правило разбора пачки — без базы."""

    def test_order_is_by_visual_time_not_arrival(self):
        late = cmd(at=at_s(5))
        early = cmd(at=at_s(1))
        late.id, early.id = 1, 2          # в очередь легла раньше поздняя
        self.assertEqual([c.id for c in order_batch([late, early])], [2, 1])

    def test_ties_are_broken_deterministically(self):
        a, b = cmd(at=at_s(0)), cmd(at=at_s(0))
        a.id, b.id = 7, 3
        self.assertEqual([c.id for c in order_batch([a, b])], [3, 7])

    def test_first_wins_among_exclusive_commands(self):
        first = cmd(task=1, at=at_s(1))
        second = cmd(task=2, at=at_s(2))
        run, rejected = plan_batch([second, first])
        self.assertEqual(run, [first])
        self.assertEqual(len(rejected), 1)
        self.assertIn("занят", rejected[0].reason)

    def test_different_prices_do_not_block_each_other(self):
        a = cmd(price=1, at=at_s(1))
        b = cmd(price=2, at=at_s(2))
        run, rejected = plan_batch([a, b])
        self.assertEqual(len(run), 2)
        self.assertEqual(rejected, [])

    def test_rule_applies_to_assignments_too(self):
        """Решение админа от 14.09.2026: «кто первый» — для ВСЕХ команд без исключений.

        Две смены статуса ОДНОГО объекта сюда не доходят — они схлопываются ещё в очереди,
        и в пачке остаётся одна. Под правило попадают команды к РАЗНЫМ объектам одного
        прайса.
        """
        a = cmd(kind=CommandKind.SET_TASK_STATUS, task=1, at=at_s(1))
        b = cmd(kind=CommandKind.SET_TASK_STATUS, task=2, at=at_s(2))
        run, rejected = plan_batch([a, b])
        self.assertEqual(run, [a])
        self.assertEqual(len(rejected), 1)

    def test_assignment_is_blocked_by_a_busy_price(self):
        """§5.1: пока с прайсом работает один админ, все его задачи закрыты для других."""
        a = cmd(kind=CommandKind.EDIT_TASK_DESCRIPTION, task=1)
        run, rejected = plan_batch([a], busy_prices={1})
        self.assertEqual(run, [])
        self.assertIn("другим администратором", rejected[0].reason)

    def test_already_locked_price_rejects_work_commands(self):
        a = cmd(price=1)
        run, rejected = plan_batch([a], busy_prices={1})
        self.assertEqual(run, [])
        self.assertIn("другим администратором", rejected[0].reason)

    def test_command_without_price_is_not_blocked(self):
        """«Принять прайс» ещё не относится ни к какому прайсу."""
        a = Command(kind=CommandKind.SUBMIT_PRICE, created_at=at_s(0))
        run, _ = plan_batch([a], busy_prices={1, 2})
        self.assertEqual(run, [a])

    def test_rebuild_and_execute_compete_for_the_same_price(self):
        run_first = cmd(kind=CommandKind.EXECUTE_TASK, task=1, at=at_s(1))
        rebuild = cmd(kind=CommandKind.REBUILD_TASKS, at=at_s(2))
        run, rejected = plan_batch([rebuild, run_first])
        self.assertEqual(run, [run_first])
        self.assertEqual(rejected[0].command.kind, CommandKind.REBUILD_TASKS)


class ClockTest(unittest.TestCase):
    """Сбитые часы визуала (§7).

    Порядок решает время создания на стороне визуала, но у 1С свой сервер. Если её часы
    уходят, она либо всегда выигрывает, либо всегда проигрывает — и честный порядок
    превращается в фикцию. Telegram этой болезни не подвержен: метку ставит тот же процесс.
    """

    def test_no_offset_keeps_the_stamp(self):
        got, note = sort_time(at_s(10), 0.0, NOW)
        self.assertEqual(got, at_s(10))
        self.assertEqual(note, "")

    def test_offset_is_subtracted(self):
        """Часы 1С спешат на 120 с — метка приводится к нашим."""
        got, note = sort_time(at_s(130), offset_seconds=120, agent_now=NOW)
        self.assertEqual(got, at_s(10))
        self.assertEqual(note, "")

    def test_lagging_clock_is_corrected_too(self):
        got, _ = sort_time(at_s(-130), offset_seconds=-120, agent_now=NOW)
        self.assertEqual(got, at_s(-10))

    def test_corrected_stamps_restore_the_true_order(self):
        """Главное: после поправки выигрывает тот, кто ДЕЙСТВИТЕЛЬНО нажал раньше."""
        # 1С спешит на час: её «10:00:05» это на самом деле 09:00:05.
        onec, _ = sort_time(at_s(3605), offset_seconds=3600, agent_now=NOW)
        telegram, _ = sort_time(at_s(10), offset_seconds=0, agent_now=NOW)
        self.assertLess(onec, telegram)

    def test_wild_stamp_falls_back_to_our_clock(self):
        got, note = sort_time(at_s(99999), 0.0, NOW)
        self.assertEqual(got, NOW.isoformat())
        self.assertIn("разошлись", note)

    def test_unknown_clock_falls_back_without_pretending(self):
        got, note = sort_time(at_s(10), 0.0, NOW, trust=False)
        self.assertEqual(got, NOW.isoformat())
        self.assertIn("неизвестны", note)

    def test_old_but_honest_stamp_is_kept(self):
        """Команда могла честно пролежать в очереди 1С, пока агент был выключен.

        Такая метка ВЕРНА, и портить её нельзя — иначе порядок команд, накопившихся за
        простой, схлопнется в момент подъёма.
        """
        got, note = sort_time(at_s(-1800), 0.0, NOW)
        self.assertEqual(got, at_s(-1800))
        self.assertEqual(note, "")

    def test_broken_and_missing_stamps(self):
        self.assertIn("не разобрана", sort_time("вчера", 0.0, NOW)[1])
        self.assertIn("метки нет", sort_time("", 0.0, NOW)[1])

    def test_naive_stamp_is_treated_as_utc(self):
        got, note = sort_time(NOW.replace(tzinfo=None).isoformat(), 0.0, NOW)
        self.assertEqual(note, "")


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
        await self.q.put(cmd(task=1, at=at_s(9)))
        await self.q.put(cmd(task=2, at=at_s(1)))
        self.assertEqual([c.task_id for c in await self.q.pending()], [2, 1])

    async def test_status_change_overwrites_instead_of_queueing_twice(self):
        """В модель приезжает последнее решение, а не цепочка промежуточных."""
        await self.q.put(cmd(kind=CommandKind.SET_TASK_STATUS, task=5, status="выполнена"))
        await self.q.put(cmd(kind=CommandKind.SET_TASK_STATUS, task=5,
                             at=at_s(30), status="к обработке"))

        pending = await self.q.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].payload["status"], "к обработке")
        self.assertEqual(pending[0].created_at, at_s(30))

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

    async def test_execute_commands_also_coalesce(self):
        """Решение админа 14.09.2026: новая команда замещает лежащую того же вида.

        Два «выполни задачу 5» подряд — одно намерение, запускать её дважды не нужно.
        """
        await self.q.put(cmd(task=5))
        await self.q.put(cmd(task=5, at=at_s(30)))
        self.assertEqual(len(await self.q.pending()), 1)

    async def test_edit_then_execute_becomes_one_command_carrying_the_text(self):
        """Сценарий админа: правит описание и тут же жмёт «Выполнить», между опросами.

        Ключ замещения — ОБЪЕКТ, не вид, поэтому команды сливаются в одну. Иначе обе попали
        бы в одну пачку, и правило «кто первый» отклонило бы «Выполнить» с «прайс занят» —
        штатный порядок работы ломался бы на ровном месте.

        Полезная нагрузка при этом объединяется: агент получает «выполнить задачу 5» СО
        СВЕЖИМ текстом задания, а не со старым.
        """
        await self.q.put(cmd(kind=CommandKind.EDIT_TASK_DESCRIPTION, task=5, text="новое"))
        await self.q.put(cmd(kind=CommandKind.EXECUTE_TASK, task=5, at=at_s(1)))

        pending = await self.q.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].kind, CommandKind.EXECUTE_TASK)
        self.assertEqual(pending[0].payload["text"], "новое")

    async def test_newer_payload_wins_on_the_same_key(self):
        await self.q.put(cmd(kind=CommandKind.EDIT_TASK_DESCRIPTION, task=5, text="раз"))
        await self.q.put(cmd(kind=CommandKind.EDIT_TASK_DESCRIPTION, task=5,
                             at=at_s(1), text="два"))
        self.assertEqual((await self.q.pending())[0].payload["text"], "два")

    async def test_commands_on_different_tasks_do_not_merge(self):
        """Замещение работает ТОЛЬКО по конкретной задаче."""
        await self.q.put(cmd(kind=CommandKind.EDIT_TASK_DESCRIPTION, task=5, text="новое"))
        await self.q.put(cmd(kind=CommandKind.EXECUTE_TASK, task=6, at=at_s(1)))
        self.assertEqual(len(await self.q.pending()), 2)

    async def test_task_command_does_not_merge_with_a_price_command(self):
        """Задача и прайс — разные объекты: «пересобрать задачи» не съедает правку."""
        await self.q.put(cmd(kind=CommandKind.EDIT_TASK_DESCRIPTION, task=5, text="новое"))
        await self.q.put(cmd(kind=CommandKind.REBUILD_TASKS, price=1, at=at_s(1)))
        self.assertEqual(len(await self.q.pending()), 2)

    async def test_submit_price_never_coalesces(self):
        """У неё нет объекта: две такие команды — два разных файла."""
        await self.q.put(Command(kind=CommandKind.SUBMIT_PRICE, created_at=at_s(0)))
        await self.q.put(Command(kind=CommandKind.SUBMIT_PRICE, created_at=at_s(1)))
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
        await self.q.put(cmd(task=2, at=at_s(30)))
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
            await self.q.put(cmd(task=i, at=at_s(i)))
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
        await self.q.put(cmd(task=1, actor="admin-1", at=at_s(2),
                             source="telegram"))
        await self.q.put(cmd(task=2, actor="admin-2", at=at_s(1),
                             source="1c"))

        run, rejected = plan_batch(await self.q.take())

        # Выиграл тот, кто НАЖАЛ раньше, а не тот, чей провайдер опросили первым.
        self.assertEqual(run[0].actor, "admin-2")
        self.assertEqual(rejected[0].command.actor, "admin-1")


if __name__ == "__main__":
    unittest.main()
