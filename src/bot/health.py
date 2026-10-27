"""Доступность Telegram и Anthropic с сервера агента — для формы 1С (решение админа 27.10.2026).

27.10.2026 провайдер сервера начал резать трафик к обоим: с ночи пропал Telegram, к вечеру
Anthropic. Узнавали об этом по косвенным признакам — сообщения не доходят, сборка задач
срывается «Connection error», — и каждый раз разбирали журнал. Теперь агент сам проверяет
оба сервиса раз в минуту, и форма показывает «доступен» или «недоступен с <время, когда
был доступен в последний раз>».

ПРОВЕРЯЕМ ТЕМ ЖЕ ПУТЁМ, КАКИМ РАБОТАЕМ. Telegram — запросом бота `getMe`, Anthropic —
списком моделей через SDK: оба идут тем же клиентом, теми же настройками прокси и с тем
же ключом, что и настоящая работа. Пинг голого адреса показал бы «доступен» там, где бот
не может ничего (и наоборот, когда заработает прокси). Ни один запрос не стоит токенов.

ДВА ПРОВАЛА ПОДРЯД — «НЕДОСТУПЕН», ОДИН УСПЕХ — «ДОСТУПЕН». Связь здесь рвётся и сама по
себе (одиночные таймауты бывают и в хороший день), и надпись, мигающая красным от каждого
из них, перестала бы что-либо значить. Обратно — сразу: доступность, однажды
подтверждённая, это факт, а не подозрение.

В 1С УЕЗЖАЕТ ТОЛЬКО СМЕНА СОСТОЯНИЯ, а не каждая проверка: снимок едет, когда менялось
состояние, и ежеминутная отправка «всё ещё доступен» гоняла бы его зря. Время последней
доступности при этом нужно лишь у недоступного сервиса — а у него оно и не меняется.
Последнюю доступность помним в базе: после перезапуска «недоступен с» не должно
превратиться в «недоступен с неизвестно когда».
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

CHECK_PERIOD = 60          # секунд между проверками
PROBE_TIMEOUT = 15         # секунд на одну проверку
FAILS_TO_DOWN = 2          # провалов подряд, после которых сервис «недоступен»

TELEGRAM = "telegram"
ANTHROPIC = "anthropic"
SETTING_PREFIX = "health_last_ok_"

Probe = Callable[[], Awaitable[bool]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ServiceState:
    name: str
    available: bool | None = None      # None — ещё не проверяли
    last_ok: str | None = None         # ISO UTC последней успешной проверки
    fails: int = 0

    def view(self) -> dict:
        return {"service": self.name, "available": bool(self.available),
                "last_ok": self.last_ok}


class HealthWatch:
    """Периодическая проверка сервисов. `on_change(states)` зовётся при смене состояния."""

    def __init__(self, probes: dict[str, Probe], settings=None,
                 on_change: Callable[[list[dict]], None] | None = None):
        self._probes = probes
        self._settings = settings
        self._on_change = on_change
        self.states = {name: ServiceState(name) for name in probes}

    async def load(self) -> None:
        """Поднять из базы время последней доступности."""
        if self._settings is None:
            return
        for state in self.states.values():
            try:
                state.last_ok = await self._settings.get_setting(
                    SETTING_PREFIX + state.name) or None
            except Exception:                           # noqa: BLE001
                logger.warning("Не прочитал последнюю доступность %s", state.name,
                               exc_info=True)

    def snapshot(self) -> list[dict]:
        """Состояние для 1С. Непроверенные сервисы не отдаются — о них нечего сказать."""
        return [s.view() for s in self.states.values() if s.available is not None]

    async def check_once(self) -> bool:
        """Проверить все сервисы. Истина — состояние хоть одного сменилось."""
        results = await asyncio.gather(*(self._probe(name) for name in self.states))
        changed = False
        for name, ok in zip(self.states, results):
            changed |= await self._apply(self.states[name], ok)
        if changed and self._on_change is not None:
            self._on_change(self.snapshot())
        return changed

    async def run_forever(self) -> None:
        await self.load()
        while True:
            try:
                await self.check_once()
            except Exception:                           # noqa: BLE001
                logger.warning("Проверка доступности сервисов сорвалась", exc_info=True)
            await asyncio.sleep(CHECK_PERIOD)

    async def _probe(self, name: str) -> bool:
        try:
            return bool(await asyncio.wait_for(self._probes[name](), PROBE_TIMEOUT + 5))
        except Exception:                               # noqa: BLE001
            return False

    async def _apply(self, state: ServiceState, ok: bool) -> bool:
        before = state.available
        if ok:
            state.fails = 0
            state.available = True
            state.last_ok = _now()
            await self._remember(state)
        else:
            state.fails += 1
            # Первая проверка после старта провалилась — ждать второго провала, чтобы
            # назвать сервис недоступным, незачем: «доступен» мы о нём не знали ни разу.
            if state.fails >= FAILS_TO_DOWN or before is None:
                state.available = False
        if state.available != before:
            logger.info("%s: %s", state.name,
                        "доступен" if state.available else
                        f"недоступен, последний раз доступен {state.last_ok or 'неизвестно'}")
            return True
        return False

    async def _remember(self, state: ServiceState) -> None:
        if self._settings is None:
            return
        try:
            await self._settings.set_setting(SETTING_PREFIX + state.name, state.last_ok)
        except Exception:                               # noqa: BLE001
            logger.warning("Не записал последнюю доступность %s", state.name, exc_info=True)


def telegram_probe(bot) -> Probe:
    """`getMe` тем же ботом, что работает: те же сессия и прокси."""
    async def probe() -> bool:
        await bot.get_me(request_timeout=PROBE_TIMEOUT)
        return True
    return probe


def anthropic_probe(api_key: str) -> Probe:
    """Список моделей через SDK. Ответ API с любым кодом — значит, сервис достижим.

    Ошибка ключа (401) или лимита (429) — это ответ Anthropic, а не обрыв: сеть до него
    есть. Недоступным считаем только обрыв соединения и таймаут.
    """
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0,
                                      timeout=PROBE_TIMEOUT)

    async def probe() -> bool:
        try:
            await client.models.list(limit=1)
        except anthropic.APIStatusError:
            return True
        return True
    return probe
