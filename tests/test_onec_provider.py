"""Провайдер 1С (specs/1c-model-form.md §3).

Клиент 1С поддельный: живой обмен проверяет `tests/integration_model_state.py`, а здесь —
поведение провайдера, которое живой базой как раз НЕ проверить: что снимок не уезжает
впустую, что схлопнутые команды закрываются обе, что отказ доезжает до формы причиной.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.model.commands import Command, CommandKind
from src.model.events import Event, EventKind
from src.onec.model_provider import (OnecProvider, actor_label, actor_of,
                                     clock_offset)
from src.storage.command_queue import CommandQueue


class FakeOnec:
    """Считает вызовы и отдаёт заранее заданные команды."""

    def __init__(self, commands=None, server_time=None):
        self.commands = list(commands or [])
        self.server_time = server_time or datetime.now(timezone.utc).isoformat()
        self.states = []          # пачки, ушедшие в agent-commands-state
        self.snapshots = []       # снимки, ушедшие в set-model-state
        self.polls = 0

    def agent_commands(self):
        self.polls += 1
        answer = {"server_time": self.server_time, "commands": self.commands}
        self.commands = []        # 1С отдаёт ждущие один раз: дальше они «приняты»
        return answer

    def agent_commands_state(self, items):
        self.states.append(list(items))
        return {"updated": len(items), "results": []}

    def set_model_state(self, prices):
        self.snapshots.append(prices)
        return {"version": len(self.snapshots), "prices": len(prices), "changed": 1}


class FakeService:
    def __init__(self, prices=None):
        self._prices = list(prices or [])

    @property
    def prices(self):
        return list(self._prices)

    def lock_of(self, price_id):
        return None


def command(external="c1", kind="выполнить задачу", price_id=1, task_id=5,
            actor="Иванов", payload=None, created_at=None):
    return {"id": external, "kind": kind, "price_id": price_id, "task_id": task_id,
            "actor": actor, "payload": payload or {},
            "created_at": created_at or "2026-09-14T12:00:00"}


class ClockTest(unittest.TestCase):

    def test_offset_absorbs_the_time_zone(self):
        """1С шлёт местное время без зоны, и разница поясов обязана попасть в смещение:
        иначе пришлось бы хранить пояс сервера отдельной настройкой."""
        agent = datetime(2026, 9, 14, 9, 0, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(clock_offset("2026-09-14T12:00:00", agent), 3 * 3600, 0)

    def test_offset_is_none_when_unmeasurable(self):
        self.assertIsNone(clock_offset(None))
        self.assertIsNone(clock_offset("не время вовсе"))

    def test_actor_is_namespaced_and_readable(self):
        self.assertEqual(actor_of("Петров"), "1c:Петров")
        self.assertEqual(actor_label("1c:Петров"), "Петров")
        # Голый идентификатор Telegram человеку ничего не говорит
        self.assertEqual(actor_label("8123456"), "Telegram 8123456")
        self.assertEqual(actor_label(""), "")


class ProviderTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.queue = CommandQueue(Path(self._dir.name) / "q.db")
        await self.queue.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    def make(self, onec, prices=None):
        return OnecProvider(onec, FakeService(prices))

    # ------------------------------------------------------------------ команды

    async def test_command_reaches_the_queue_and_is_acked(self):
        onec = FakeOnec([command()])
        provider = self.make(onec)
        await provider.collect(self.queue)

        pending = await self.queue.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].kind, CommandKind.EXECUTE_TASK)
        self.assertEqual(pending[0].source, "1c")
        self.assertEqual(pending[0].actor, "1c:Иванов")
        self.assertEqual(pending[0].task_id, 5)

        acked = [i for batch in onec.states for i in batch]
        self.assertEqual([(i["id"], i["state"]) for i in acked], [("c1", "принята")])

    async def test_unknown_kind_is_refused_not_left_hanging(self):
        """Иначе на форме навсегда горело бы колесико ожидания."""
        onec = FakeOnec([command(kind="поправить всё")])
        await self.make(onec).collect(self.queue)

        self.assertEqual(await self.queue.pending(), [])
        acked = [i for batch in onec.states for i in batch]
        self.assertEqual(acked[0]["state"], "отклонена")
        self.assertIn("неизвестный вид", acked[0]["message"])

    async def test_zero_ids_mean_no_object(self):
        """У регистра числовой ресурс, пустого значения в нём не бывает: ноль — это
        «команда про прайс целиком», а не задача номер ноль."""
        onec = FakeOnec([command(kind="пересобрать задачи", price_id=3, task_id=0)])
        await self.make(onec).collect(self.queue)

        pending = await self.queue.pending()
        self.assertEqual(pending[0].price_id, 3)
        self.assertIsNone(pending[0].task_id)

    async def test_clock_skew_is_corrected(self):
        """Часы 1С уходят на час вперёд — метка обязана приехать к нашим."""
        agent_now = datetime.now(timezone.utc)
        onec = FakeOnec(
            [command(created_at=(agent_now + timedelta(hours=1)).isoformat())],
            server_time=(agent_now + timedelta(hours=1)).isoformat())
        await self.make(onec).collect(self.queue)

        stamp = datetime.fromisoformat((await self.queue.pending())[0].sort_at)
        self.assertLess(abs((stamp - agent_now).total_seconds()), 60)

    # -------------------------------------------------------------------- снимок

    async def test_snapshot_goes_once_and_not_again(self):
        """В простое оборот обязан стоить один GET: снимок без изменений не шлётся."""
        onec = FakeOnec()
        provider = self.make(onec)

        await provider.collect(self.queue)
        self.assertEqual(len(onec.snapshots), 1)      # первый оборот выравнивает зеркало

        await provider.collect(self.queue)
        self.assertEqual(len(onec.snapshots), 1)      # менять было нечего

        await provider.notify(Event(EventKind.TASK_STATUS, text="что-то поменялось"))
        await provider.collect(self.queue)
        self.assertEqual(len(onec.snapshots), 2)

    async def test_failed_snapshot_is_retried(self):
        """Не доехавший из-за сети снимок обязан поехать снова: иначе форма замрёт на
        старом состоянии, ничем этого не показав."""
        onec = FakeOnec()
        broken = []

        def fail(prices):
            broken.append(prices)
            raise RuntimeError("сеть моргнула")

        onec.set_model_state = fail
        provider = self.make(onec)
        await provider.collect(self.queue)
        self.assertEqual(len(broken), 1)

        await provider.collect(self.queue)
        self.assertEqual(len(broken), 2)

    async def test_snapshot_shape(self):
        from src.model.enums import TaskKind, TaskStatus
        from src.model.price import Price, SupplierPrice
        from src.model.refs import Ref, TaskAddress
        from src.model.task import PriceTask

        task = PriceTask(kind=TaskKind.CHANGE_PRICES,
                         address=TaskAddress(tm=Ref.make(names=["Egger"]),
                                             subject=Ref.make(names=["Vintage"])),
                         description="проверить цены", id=7)
        task.complete(TaskStatus.DONE, "готово")
        price = Price(supplier_price=SupplierPrice(supplier_id=1, file_id=1,
                                                   file_path="p.xlsx",
                                                   filename="Прайс.xlsx"),
                      id=3, tasks=[task])

        snapshot = await self.make(FakeOnec(), [price]).snapshot()
        self.assertEqual(len(snapshot), 1)
        row = snapshot[0]
        self.assertEqual(row["id"], 3)
        self.assertEqual(row["file"], "Прайс.xlsx")
        self.assertTrue(row["ready"])
        self.assertEqual(row["locked_by"], "")
        item = row["tasks"][0]
        self.assertEqual(item["id"], 7)
        self.assertEqual(item["kind"], "изменение цен")
        # `Порядок` в 1С считается от единицы, а `kind.order` — от нуля
        self.assertEqual(item["order"], TaskKind.CHANGE_PRICES.order + 1)
        self.assertEqual(item["status"], "выполнена")
        self.assertEqual(item["result"], "готово")
        # отметка о прогоне едет в 1С отдельно от `done_at`: колонка «Выполнялась»
        self.assertEqual(item["run_at"], task.run_at)
        # снимок обязан сериализоваться: Неопределено в нём ломало бы запись в 1С
        json.dumps(snapshot, ensure_ascii=False)

    # ------------------------------------------------------------- судьба команд

    async def test_finished_command_is_reported_done(self):
        onec = FakeOnec([command()])
        provider = self.make(onec)
        await provider.collect(self.queue)

        taken = await self.queue.take()
        await self.queue.done(taken[0].id)

        await provider.collect(self.queue)
        acked = [i for batch in onec.states for i in batch]
        self.assertEqual([(i["id"], i["state"]) for i in acked],
                         [("c1", "принята"), ("c1", "выполнена")])

    async def test_rejection_reaches_the_form_with_its_reason(self):
        onec = FakeOnec([command()])
        provider = self.make(onec)
        await provider.collect(self.queue)

        await provider.notify(Event(EventKind.COMMAND_REJECTED, price_id=1, task_id=5,
                                    actor="1c:Иванов", text="прайс занят: Петров"))
        taken = await self.queue.take()
        await self.queue.done(taken[0].id)

        await provider.collect(self.queue)
        last = [i for batch in onec.states for i in batch][-1]
        self.assertEqual(last["state"], "отклонена")
        self.assertIn("Петров", last["message"])

    async def test_coalesced_commands_are_both_closed(self):
        """Две команды по одной задаче схлопываются в очереди в ОДНУ строку. Закрыть надо
        обе — иначе в форме навсегда останется гореть колесико по исчезнувшей."""
        provider = self.make(FakeOnec())

        # первый оборот принёс одну команду, второй — вторую по той же задаче
        provider._onec.commands = [command(external="c1")]
        await provider.collect(self.queue)
        provider._onec.commands = [command(external="c2", kind="сменить статус задачи")]
        await provider.collect(self.queue)

        pending = await self.queue.pending()
        self.assertEqual(len(pending), 1, "команды обязаны схлопнуться")

        await self.queue.done(pending[0].id)
        await provider.collect(self.queue)

        final = {i["id"]: i["state"] for batch in provider._onec.states for i in batch
                 if i["state"] in ("выполнена", "отклонена")}
        self.assertEqual(final, {"c1": "выполнена", "c2": "выполнена"})

    async def test_snapshot_goes_before_the_outcome(self):
        """Порядок важен для глаз админа, а не для машины.

        Форма гасит кнопки, пока команда не закрыта. Закрыв её раньше снимка, мы на
        мгновение отдали бы живые кнопки поверх СТАРЫХ данных: задача выглядит
        неизменившейся, и нажатие кажется пропавшим.
        """
        onec = FakeOnec([command()])
        order = []
        onec.set_model_state = lambda prices: order.append("снимок") or {"version": 1}
        onec.agent_commands_state = lambda items: order.append(
            "судьба:" + items[0]["state"]) or {"updated": len(items)}

        provider = self.make(onec)
        await provider.collect(self.queue)
        taken = await self.queue.take()
        await self.queue.done(taken[0].id)
        order.clear()

        await provider.notify(Event(EventKind.TASK_STATUS, text="задача закрыта"))
        await provider.collect(self.queue)

        self.assertEqual(order[:2], ["снимок", "судьба:выполнена"])

    async def test_still_pending_command_is_not_reported_done(self):
        onec = FakeOnec([command()])
        provider = self.make(onec)
        await provider.collect(self.queue)
        await provider.collect(self.queue)

        states = [i["state"] for batch in onec.states for i in batch]
        self.assertEqual(states, ["принята"])

    # -------------------------------------------------------------------- отказы

    async def test_broken_1c_does_not_break_the_turn(self):
        """Цикл ловит исключения сам, но признак «менялось» обязан пережить сбой."""
        class Dead:
            def agent_commands(self):
                raise RuntimeError("1С недоступна")

            def agent_commands_state(self, items):
                raise RuntimeError("1С недоступна")

            def set_model_state(self, prices):
                raise RuntimeError("1С недоступна")

        provider = self.make(Dead())
        await provider.collect(self.queue)      # не должно бросить
        self.assertTrue(provider._dirty)

    async def test_missing_objects_are_logged_not_swallowed(self):
        onec = FakeOnec()
        onec.agent_commands = lambda: {"server_time": "2026-09-14T12:00:00",
                                       "commands": [],
                                       "missing": ["РегистрСведений.ии_МодельКоманды"]}
        with self.assertLogs("src.onec.model_provider", level="ERROR") as logs:
            await self.make(onec).collect(self.queue)
        self.assertIn("ии_МодельКоманды", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
