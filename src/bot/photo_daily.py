"""Ежедневная проверка фото и еженедельное напоминание админу (решение 30.09.2026).

**ПОЧЕМУ ПРОВЕРКА ЕЖЕДНЕВНАЯ, А НАПОМИНАНИЕ ЕЖЕНЕДЕЛЬНОЕ.** Это разные вещи, и путать их
нельзя. Проверка наполняет журнал — от её частоты зависит точность ответа «когда появилось
фото»: раз в неделю давала бы срок с точностью до недели. Стоит она 54 секунды и ноль
токенов. Напоминание же читает человек, и еженедельное — предел, за которым письмо
перестают открывать; ежедневный список почти без изменений обесценил бы сам себя.

**ДАЙДЖЕСТ ИДЁТ ПО ОТДЕЛЬНОМУ СПИСКУ ПОДПИСЧИКОВ** (`storage/photo_subscribers`, решение
админа 30.09.2026), и админ в нём — такой же участник: нет его в списке, дайджест ему не
приходит. Доступ к боту и подписка на рассылку — разные вещи: пользоваться ботом нужно
всем менеджерам, а сводку про фото получать тем, кто этим занимается. Пустой список значит
«никому», а не «всем».

**ПУСТОЙ ДАЙДЖЕСТ НЕ ОТПРАВЛЯЕТСЯ**: ждать фото нечему — молчим. Еженедельное «всё в
порядке» через месяц перестают читать вместе с теми письмами, в которых что-то есть.

**ПРОПУЩЕННЫЙ ДЕНЬ ДОГОНЯЕТСЯ ПРИ СТАРТЕ.** Бота перезапускают среди дня, и проверка,
привязанная только к часу, в этот день просто не случилась бы. Поэтому при подъёме мы
смотрим журнал: если сегодня наблюдений не было — проверяем сразу.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from src.website_tool import photo_report, photo_scan

logger = logging.getLogger(__name__)

#: Час местного времени сервера, в который идёт ежедневная проверка. Раннее утро: 1С в это
#: время свободна, а результат готов к началу рабочего дня.
CHECK_HOUR = 7

#: День недели для напоминания (0 — понедельник): неделя начинается с разбора хвостов.
REMIND_WEEKDAY = 0

#: Окно новизны для ежедневного обхода. Три месяца — то же, что у вопроса менеджера; всё,
#: что старше, всё равно остаётся под наблюдением через журнал.
WATCH_MONTHS = 3


def _months_ago(months: int, today: date) -> str:
    from src.agent.tools import _months_ago as calc
    return calc(months, today)


def next_run(now: datetime, hour: int = CHECK_HOUR) -> datetime:
    """Ближайший момент проверки строго ПОСЛЕ `now`.

    Время передаётся явно: иначе тест на расписание пришлось бы писать через ожидание.
    """
    today = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return today if today > now else today + timedelta(days=1)


async def run_once(onec, store, bot=None, subscribers=None,
                   today: date | None = None) -> None:
    """Один проход: обойти, записать в журнал, по понедельникам разослать дайджест."""
    day = today or date.today()
    stamp = day.isoformat()
    since = _months_ago(WATCH_MONTHS, day)

    extra = [{"ref": w.ref, "site_id": w.site_id, "tm": w.tm, "collection": w.collection,
              "name": w.name, "created": w.created} for w in await store.open_ids()]
    found = await photo_scan.scan(onec, since=since, extra=extra)
    if found.problem:
        # Сломанный обход не имеет права молча стереть историю: журнал остаётся как был,
        # а подписчики узнают причину — иначе прогресс замрёт, и понять почему будет негде.
        logger.warning("Проверка фото не прошла: %s", found.problem)
        await _send(bot, subscribers, f"Проверка фото не прошла: {found.problem}")
        return

    await store.observe(found.observations, today=stamp)
    logger.info("Проверка фото: %d позиций, без фото %d",
                found.checked, len(found.rows))

    if day.weekday() != REMIND_WEEKDAY:
        return

    text = photo_report.digest(await store.progress(today=stamp),
                               await store.waiting(), today=stamp)
    if text:
        await _send(bot, subscribers, text)


async def _send(bot, subscribers, text: str) -> None:
    """Разослать подписчикам. ПУСТОЙ СПИСОК ЗНАЧИТ «НИКОМУ», а не «всем»: обратное
    умолчание однажды разошлёт каталог всем подряд после неудачной миграции.

    Админ тут не особенный: нет его в списке — дайджест ему не приходит (решение админа
    30.09.2026). Поэтому о недоставке пишем В ЖУРНАЛ, а не ему в личку: сообщение тому,
    кто на рассылку не подписывался, — ровно то, чего это правило и не допускает.
    """
    if bot is None or subscribers is None:
        return
    people = await subscribers.list_all()
    if not people:
        logger.info("Дайджест по фото никому не отправлен: список подписчиков пуст")
        return
    for person in people:
        await _say(bot, person.telegram_id, text)


async def _say(bot, chat_id: int, text: str) -> None:
    """Сбой отправки не имеет права ронять цикл: следующая проверка всё равно состоится."""
    try:
        for i in range(0, len(text), 4096):
            await bot.send_message(chat_id, text[i:i + 4096])
    except Exception:                                   # noqa: BLE001
        logger.exception("Не удалось отправить сообщение о фото")


async def run_forever(onec, store, bot=None, subscribers=None) -> None:
    """Фоновая задача: догнать пропущенное и дальше ходить по расписанию."""
    try:
        if await store.last_run() != date.today().isoformat():
            await run_once(onec, store, bot, subscribers)
    except Exception:                                   # noqa: BLE001
        logger.exception("Проверка фото при старте сорвалась")

    while True:
        now = datetime.now()
        await asyncio.sleep(max(1.0, (next_run(now) - now).total_seconds()))
        try:
            await run_once(onec, store, bot, subscribers)
        except Exception:                               # noqa: BLE001
            # Сорвавшийся день — не повод прекращать наблюдение: завтра попробуем снова.
            logger.exception("Ежедневная проверка фото сорвалась")
