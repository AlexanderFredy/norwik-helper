"""Доступность Telegram и Anthropic для формы 1С (src/bot/health.py, 27.10.2026)."""
import unittest

from src.bot import health


class Settings:
    def __init__(self, values=None):
        self.values = dict(values or {})

    async def get_setting(self, key, default=""):
        return self.values.get(key, default)

    async def set_setting(self, key, value):
        self.values[key] = value


def watch(answers, settings=None):
    """`answers` — имя сервиса → список исходов проверок по очереди."""
    seen = []

    def probe(name):
        async def run():
            ok = answers[name].pop(0)
            if isinstance(ok, Exception):
                raise ok
            return ok
        return run

    w = health.HealthWatch({n: probe(n) for n in answers}, settings=settings,
                           on_change=seen.append)
    return w, seen


class HealthWatchTest(unittest.IsolatedAsyncioTestCase):

    async def test_first_result_is_reported(self):
        w, seen = watch({"telegram": [True], "anthropic": [ConnectionError()]})
        self.assertTrue(await w.check_once())
        by_name = {s["service"]: s for s in seen[-1]}
        self.assertTrue(by_name["telegram"]["available"])
        self.assertIsNotNone(by_name["telegram"]["last_ok"])
        self.assertFalse(by_name["anthropic"]["available"], "провал на старте — сразу недоступен")

    async def test_single_drop_does_not_flip_to_unavailable(self):
        """Одиночный таймаут бывает и в хороший день — надпись не должна мигать."""
        w, seen = watch({"anthropic": [True, False, True]})
        await w.check_once()
        self.assertFalse(await w.check_once())
        self.assertTrue(w.states["anthropic"].available)
        self.assertEqual(len(seen), 1)

    async def test_two_drops_flip_and_keep_last_ok(self):
        w, seen = watch({"anthropic": [True, False, False, False]})
        await w.check_once()
        last_ok = w.states["anthropic"].last_ok
        await w.check_once()
        self.assertTrue(await w.check_once())
        self.assertEqual(seen[-1], [{"service": "anthropic", "available": False,
                                     "last_ok": last_ok}])
        self.assertFalse(await w.check_once(), "пока недоступен — в 1С ничего не едет")

    async def test_recovery_is_immediate(self):
        w, _ = watch({"telegram": [False, True]})
        await w.check_once()
        self.assertTrue(await w.check_once())
        self.assertTrue(w.states["telegram"].available)

    async def test_last_ok_survives_restart(self):
        settings = Settings({"health_last_ok_telegram": "2026-10-27T11:26:05+00:00"})
        w, seen = watch({"telegram": [False]}, settings)
        await w.load()
        await w.check_once()
        self.assertEqual(seen[-1][0]["last_ok"], "2026-10-27T11:26:05+00:00")

    async def test_success_is_remembered(self):
        settings = Settings()
        w, _ = watch({"anthropic": [True]}, settings)
        await w.check_once()
        self.assertEqual(settings.values["health_last_ok_anthropic"],
                         w.states["anthropic"].last_ok)

    async def test_unchecked_service_is_not_sent(self):
        w, _ = watch({"telegram": [True]})
        self.assertEqual(w.snapshot(), [])


class ProviderTest(unittest.IsolatedAsyncioTestCase):

    async def test_services_ride_with_the_snapshot(self):
        from src.onec.model_provider import OnecProvider
        from tests.test_onec_provider import FakeService

        sent = {}

        class Onec:
            def set_model_state(self, prices, sheets=None, marks=None, services=None):
                sent["services"] = services
                return {"changed": 0}

        provider = OnecProvider(Onec(), FakeService())
        provider.services_changed([{"service": "telegram", "available": False,
                                    "last_ok": "2026-10-27T11:26:05+00:00"}])
        await provider._push_state()
        self.assertEqual(sent["services"][0]["service"], "telegram")


if __name__ == "__main__":
    unittest.main()
