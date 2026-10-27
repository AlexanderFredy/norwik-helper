"""Ответ, оборванный на пределе длины, — не окончательный (бой 26.10.2026, Артисана).

Сборка задач дошла до последнего круга, модель выдала 16 000 токенов без единого вызова
`add_task` и упёрлась в `max_tokens`. Цикл принял обрубок за итог — не завелось ни одной
задачи, кроме тех, что код заводит сам.
"""
import unittest

from src.agent import orchestrator as orc_mod
from src.agent.orchestrator import Orchestrator
from tests.test_token_usage import Block, FakeClient, Response, Usage, answer


class RecordingExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, name, inp):
        self.calls.append(name)
        return "готово"


def make(responses):
    executor = RecordingExecutor()
    orc = Orchestrator(api_key="test", executor=executor)
    orc._client = FakeClient(responses)
    return orc, executor


def call(name, id_):
    return Block("tool_use", name=name, id_=id_)


class TruncatedAnswerTest(unittest.IsolatedAsyncioTestCase):

    async def test_truncation_without_calls_asks_to_continue(self):
        orc, executor = make([
            Response("max_tokens", [Block("text", "длинное рассуждение…")], Usage(o=16000)),
            Response("tool_use", [call("add_task", "t1")]),
            answer("заведено"),
        ])
        text, history = await orc.handle_turn([{"role": "user", "content": "собери"}])
        self.assertEqual(text, "заведено")
        self.assertEqual(executor.calls, ["add_task"], "после обрыва работа продолжилась")
        notes = [b for m in history if m["role"] == "user" and isinstance(m["content"], list)
                 for b in m["content"] if b.get("type") == "text"]
        self.assertTrue(any("оборвался" in b["text"] for b in notes))

    async def test_complete_calls_run_and_the_last_is_dropped(self):
        """Последний вызов мог оборваться посреди входа — его не выполняем."""
        orc, executor = make([
            Response("max_tokens", [call("add_task", "a"), call("add_task", "b"),
                                    call("add_task", "c")]),
            answer("ок"),
        ])
        _, history = await orc.handle_turn([{"role": "user", "content": "собери"}])
        self.assertEqual(executor.calls, ["add_task", "add_task"])
        results = [b["tool_use_id"] for m in history if m["role"] == "user"
                   and isinstance(m["content"], list)
                   for b in m["content"] if b.get("type") == "tool_result"]
        self.assertEqual(results, ["a", "b"], "на отброшенный вызов ответа нет")

    async def test_empty_truncation_keeps_roles_alternating(self):
        """Оборвалось одно рассуждение — пояснение дописывается к прошлой реплике
        пользователя, двух пользовательских сообщений подряд не бывает."""
        orc, _ = make([Response("max_tokens", [Block("thinking")]), answer("ок")])
        _, history = await orc.handle_turn([{"role": "user", "content": "собери"}])
        roles = [m["role"] for m in history]
        self.assertTrue(all(a != b for a, b in zip(roles, roles[1:])), roles)

    async def test_gives_up_after_the_limit(self):
        """Бесконечно просить «короче» незачем: после предела — как раньше, что пришло."""
        cut = Response("max_tokens", [Block("text", "обрубок")], Usage(o=16000))
        orc, _ = make([cut] * (orc_mod.MAX_TRUNCATIONS + 1))
        text, _ = await orc.handle_turn([{"role": "user", "content": "собери"}])
        self.assertEqual(text, "обрубок")
        self.assertEqual(orc._client.messages.calls, orc_mod.MAX_TRUNCATIONS + 1)


if __name__ == "__main__":
    unittest.main()
