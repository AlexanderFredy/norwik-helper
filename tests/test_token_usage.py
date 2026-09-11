"""Учёт расхода токенов (§9.6.3): арифметика, снятие в цикле, выборки, показ.

Цикл ручной, и каждый вызов инструмента — отдельный запрос со всей историей. Пока
`response.usage` выбрасывался, стоимость прогона была неизвестна, а значит и effort с
выбором модели настраивались вслепую. Здесь проверяется то, на чём такой учёт обычно и
ломается: недосчёт на неочевидных ветках цикла и неверный множитель кеша.
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.agent import usage
from src.agent.orchestrator import Orchestrator
from src.bot import pricing_handlers as ph
from src.storage.pricing import PricingStore


# --------------------------------------------------------------------- заглушки API

class Block:
    def __init__(self, type_, text="", name="", id_="tool-1"):
        self.type = type_
        self.text = text
        self.name = name
        self.id = id_
        self.input = {}

    def model_dump(self):
        return {"type": self.type, "text": self.text}


class Usage:
    def __init__(self, i=0, o=0, r=0, w=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = r
        self.cache_creation_input_tokens = w


class Response:
    def __init__(self, stop_reason, content, usage_=None, model="claude-opus-5"):
        self.stop_reason = stop_reason
        self.content = content
        self.usage = usage_ or Usage()
        self.model = model
        self.stop_details = None


class FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def create(self, **_kw):
        row = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return row


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


class FakeExecutor:
    async def execute(self, _name, _input):
        return "готово"


def orchestrator(responses, sink):
    orc = Orchestrator(api_key="test", executor=FakeExecutor(), on_usage=sink)
    orc._client = FakeClient(responses)
    return orc


def collector():
    rows = []

    async def sink(row):
        rows.append(row)

    return rows, sink


def answer(text="ответ", **counts):
    return Response("end_turn", [Block("text", text)], Usage(**counts))


# --------------------------------------------------------------------- арифметика

class CostTest(unittest.TestCase):

    def test_all_four_counters(self):
        # 1000×$5 + 200×$25 + 10000×$5×0.1 + 1000×$5×2 = 0.005+0.005+0.005+0.01
        self.assertAlmostEqual(
            usage.cost("claude-opus-5", input_tokens=1000, output_tokens=200,
                       cache_read=10000, cache_write=1000),
            0.025)

    def test_cache_write_is_hourly_not_five_minute(self):
        """Бот пишет в часовой кеш: ×2, а не ×1.25 из умолчания SDK.

        Ошибка на этом множителе не видна глазом — она просто занижает счёт на треть
        записи кеша, а на прайсовом прогоне префикс пишется много раз.
        """
        self.assertAlmostEqual(usage.cost("claude-opus-5", cache_write=1_000_000), 10.0)

    def test_cache_read_is_a_tenth(self):
        self.assertAlmostEqual(usage.cost("claude-opus-5", cache_read=1_000_000), 0.5)

    def test_cheaper_model_costs_less_for_the_same_tokens(self):
        opus = usage.cost("claude-opus-5", input_tokens=1_000_000)
        haiku = usage.cost("claude-haiku-4-5", input_tokens=1_000_000)
        self.assertEqual((opus, haiku), (5.0, 1.0))

    def test_unknown_model_is_not_guessed(self):
        """Приведение к ближайшему тарифу дало бы молча неверный счёт."""
        self.assertIsNone(usage.cost("claude-unknown-9", input_tokens=1000))

    def test_total_reports_unknown_models(self):
        total, unknown = usage.total_cost([
            {"model": "claude-opus-5", "input_tokens": 1_000_000},
            {"model": "claude-unknown-9", "input_tokens": 1_000_000},
        ])
        self.assertEqual(total, 5.0)
        self.assertEqual(unknown, {"claude-unknown-9"})

    def test_totals_respect_per_model_rates(self):
        total, _ = usage.total_cost([
            {"model": "claude-opus-5", "input_tokens": 1_000_000},
            {"model": "claude-haiku-4-5", "input_tokens": 1_000_000},
        ])
        self.assertEqual(total, 6.0)


class SummarizeTest(unittest.TestCase):

    def test_cache_share(self):
        s = usage.summarize([{"model": "claude-opus-5", "calls": 2, "input_tokens": 100,
                              "cache_read": 900, "cache_write": 0, "output_tokens": 10}])
        self.assertEqual(s["cache_share"], 90)

    def test_empty_journal_does_not_divide_by_zero(self):
        s = usage.summarize([])
        self.assertEqual((s["calls"], s["cache_share"], s["amount"]), (0, 0, 0.0))

    def test_small_amounts_are_not_rounded_to_zero(self):
        """Шаг прогона стоит центы, и «$0.00» в отчёте обессмыслил бы разбор."""
        self.assertEqual(usage.money(0.0004), "$0.0004")

    def test_render_block_when_nothing_spent(self):
        self.assertIn("обращений не было", usage.render_block("За сегодня", []))

    def test_render_block_names_unknown_tariff(self):
        text = usage.render_block("За сегодня", [{"model": "claude-unknown-9", "calls": 1,
                                                  "input_tokens": 10}])
        self.assertIn("тариф неизвестен", text)
        self.assertIn("claude-unknown-9", text)


# --------------------------------------------------------- снятие расхода в цикле

class RecordingTest(unittest.IsolatedAsyncioTestCase):

    async def test_plain_answer_is_recorded_with_labels(self):
        rows, sink = collector()
        orc = orchestrator([answer(i=500, o=20, r=1000, w=100)], sink)
        await orc.handle_turn([{"role": "user", "content": "привет"}],
                              usage_labels={"kind": "manager", "user_id": 42})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "manager")
        self.assertEqual(rows[0]["user_id"], 42)
        self.assertEqual(rows[0]["input_tokens"], 500)
        self.assertEqual(rows[0]["output_tokens"], 20)
        self.assertEqual(rows[0]["cache_read"], 1000)
        self.assertEqual(rows[0]["cache_write"], 100)
        self.assertEqual(rows[0]["model"], "claude-opus-5")

    async def test_pause_turn_is_also_paid_for(self):
        """Серверный web_search возвращает pause_turn и уходит на новый круг.

        Ветка идёт через `continue`, и учёт только на успешном пути дал бы тихий
        систематический недосчёт — ровно там, где запрос и стоил дороже обычного.
        """
        rows, sink = collector()
        orc = orchestrator([Response("pause_turn", [Block("text", "ищу")], Usage(i=700)),
                            answer(i=100)], sink)
        await orc.handle_turn([{"role": "user", "content": "?"}], usage_labels={})
        self.assertEqual([r["input_tokens"] for r in rows], [700, 100])

    async def test_refusal_is_also_paid_for(self):
        rows, sink = collector()
        orc = orchestrator([Response("refusal", [Block("text", "")], Usage(i=300))], sink)
        await orc.handle_turn([{"role": "user", "content": "?"}], usage_labels={})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_tokens"], 300)

    async def test_tool_step_records_the_tool_name(self):
        rows, sink = collector()
        tool = Response("tool_use", [Block("tool_use", name="search_emails")], Usage(i=900))
        orc = orchestrator([tool, answer(i=50)], sink)
        await orc.handle_turn([{"role": "user", "content": "?"}], usage_labels={})
        self.assertEqual(rows[0]["tools"], "search_emails")
        self.assertIsNone(rows[1]["tools"])

    async def test_iteration_is_numbered_from_one(self):
        rows, sink = collector()
        tool = Response("tool_use", [Block("tool_use", name="search_norwik")], Usage())
        orc = orchestrator([tool, answer()], sink)
        await orc.handle_turn([{"role": "user", "content": "?"}], usage_labels={})
        self.assertEqual([r["iteration"] for r in rows], [1, 2])

    async def test_sink_failure_does_not_break_the_turn(self):
        """Прайсовый прогон идёт по живой 1С и стоит дорого: терять его из-за сбоя
        журнала — обмен ценного на бесплатное."""
        async def broken(_row):
            raise RuntimeError("база недоступна")

        orc = orchestrator([answer("всё хорошо")], broken)
        text, _ = await orc.handle_turn([{"role": "user", "content": "?"}])
        self.assertEqual(text, "всё хорошо")

    async def test_no_sink_means_no_overhead(self):
        orc = Orchestrator(api_key="test", executor=FakeExecutor())
        orc._client = FakeClient([answer("ок")])
        text, _ = await orc.handle_turn([{"role": "user", "content": "?"}])
        self.assertEqual(text, "ок")


# --------------------------------------------------------------------- выборки

class StoreTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def write(self, **row):
        await self.store.record_usage({"kind": "pricing", "model": "claude-opus-5",
                                       "user_id": 42, **row})

    async def test_totals_are_grouped_by_model(self):
        await self.write(input_tokens=1000)
        await self.write(model="claude-haiku-4-5", input_tokens=1000)
        rows = await self.store.usage_totals()
        total, _ = usage.total_cost(rows)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(total, 0.006)      # 0.005 опус + 0.001 хайку

    async def test_kind_separates_the_two_workloads(self):
        await self.write(input_tokens=100_000)
        await self.write(kind="manager", input_tokens=1000)
        pricing = usage.summarize(await self.store.usage_totals(kind="pricing"))
        manager = usage.summarize(await self.store.usage_totals(kind="manager"))
        self.assertEqual(pricing["input_tokens"], 100_000)
        self.assertEqual(manager["input_tokens"], 1000)

    async def test_since_excludes_earlier_rows(self):
        await self.write(input_tokens=1000)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.assertEqual(usage.summarize(
            await self.store.usage_totals(since=tomorrow))["calls"], 0)
        self.assertEqual(usage.summarize(
            await self.store.usage_totals(since=yesterday))["calls"], 1)

    async def test_user_filter(self):
        await self.write(input_tokens=1000)
        await self.write(user_id=7, input_tokens=1000)
        self.assertEqual(usage.summarize(
            await self.store.usage_totals(user_id=7))["calls"], 1)

    async def test_by_tool_skips_rows_without_tools(self):
        await self.write(tools="read_price_file", input_tokens=1000)
        await self.write(input_tokens=1000)
        rows = await self.store.usage_by_tool()
        self.assertEqual([r["tools"] for r in rows], ["read_price_file"])

    async def test_counters_survive_missing_fields(self):
        """Приёмник зовут из горячего пути: строка не обязана быть полной."""
        await self.store.record_usage({"kind": "manager", "model": "claude-opus-5"})
        self.assertEqual(usage.summarize(await self.store.usage_totals())["calls"], 1)


class RunCostTest(unittest.IsolatedAsyncioTestCase):
    """«Прогон стоил …» — считается от started_at и только по своему прогону."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = PricingStore(Path(self._dir.name) / "t.db")
        await self.store.init()

    async def asyncTearDown(self):
        self._dir.cleanup()

    async def test_line_names_cost_and_cache_share(self):
        await self.store.start_run(42, "Монарх", "price.xlsx", [{"code": "T1", "name": "A"}])
        run = await self.store.get_run(42)
        await self.store.record_usage({"kind": "pricing", "user_id": 42,
                                       "model": "claude-opus-5", "input_tokens": 200_000,
                                       "cache_read": 800_000})
        line = await ph._run_cost(self.store, 42, run)
        self.assertIn("Прогон стоил", line)
        self.assertIn("80% из кеша", line)

    async def test_rows_before_the_run_are_not_counted(self):
        await self.store.record_usage({"kind": "pricing", "user_id": 42,
                                       "model": "claude-opus-5", "input_tokens": 999_999})
        await self.store.start_run(42, "Монарх", "price.xlsx", [])
        run = await self.store.get_run(42)
        # прогон начался ПОСЛЕ той строки — в его стоимость она попасть не должна
        self.assertEqual(await ph._run_cost(self.store, 42, run), "")

    async def test_no_run_means_no_line(self):
        self.assertEqual(await ph._run_cost(self.store, 42, None), "")
        self.assertEqual(await ph._run_cost(self.store, 42, {}), "")

    async def test_top_tools_orders_by_money(self):
        for tool, tokens in (("read_price_file", 10_000), ("get_1c_nomenclature", 500_000)):
            await self.store.record_usage({"kind": "pricing", "user_id": 42,
                                           "model": "claude-opus-5", "tools": tool,
                                           "input_tokens": tokens})
        top = await ph._top_tools(self.store, since=None)
        self.assertTrue(top.index("get_1c_nomenclature") < top.index("read_price_file"))

    async def test_top_tools_is_empty_without_data(self):
        self.assertEqual(await ph._top_tools(self.store, since=None), "")


if __name__ == "__main__":
    unittest.main()
