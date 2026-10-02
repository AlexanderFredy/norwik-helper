"""Провайдер 1С для цикла модели (specs/1c-model-form.md §3, §9 модели).

Второй визуал наравне с Telegram. Делает три вещи за оборот и ни одной лишней:

    1. сообщает судьбу команд, доигранных с прошлого оборота;
    2. кладёт в 1С снимок состояния — если состояние менялось;
    3. забирает ждущие команды формы и складывает их в общую очередь.

**Обе стрелки идут ОТ агента.** Обработчик HTTP-сервиса работает в своём сеансе и до
открытой формы не дотягивается — толкнуть данные в управляемую форму платформа не даёт.
Поэтому состояние оседает в 1С зеркалом, а форма читает его сама.

**Снимок отправляется только когда есть что отправлять.** Провайдер подписан на события
модели и держит признак «менялось». В простое оборот стоит один GET — тот самый лёгкий
запрос на 231 мс, ради которого эндпоинт команд не трогает Номенклатуру.

**Смещение часов измеряется каждым опросом, а не настраивается.** 1С отдаёт `server_time`,
мы сравниваем со своими часами и вычитаем разницу из меток команд. Заодно это снимает
вопрос часового пояса: 1С шлёт местное время без зоны, и разница поясов приезжает в то же
смещение, не требуя от нас знать, в каком поясе стоит сервер.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from src.model.commands import Command, CommandKind
from src.model.commands import now as commands_now
from src.model.events import Event, EventKind, Listener

logger = logging.getLogger(__name__)

SOURCE = "1c"

#: Приставка к имени пользователя 1С. По решению Ф4 админ из Telegram и админ из 1С — это
#: ДВА РАЗНЫХ человека для модели, даже если это один сотрудник. Приставка делает это
#: видимым и заодно исключает столкновение с числовым идентификатором Telegram.
ACTOR_PREFIX = "1c:"


def actor_of(name: str) -> str:
    return ACTOR_PREFIX + (name or "").strip()


def actor_label(actor: str) -> str:
    """Как показать инициатора в форме: «Петров» либо «Telegram 8123456».

    Голый идентификатор Telegram в строке «Прайс занят: 8123456» не говорит человеку
    ничего, а приставка `1c:` перед собственным именем коллеги выглядит мусором.
    """
    if not actor:
        return ""
    if actor.startswith(ACTOR_PREFIX):
        return actor[len(ACTOR_PREFIX):]
    return f"Telegram {actor}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clock_offset(server_time: str | None, agent_now: datetime | None = None) -> float | None:
    """Насколько часы 1С уходят вперёд относительно наших, в секундах. None — не измерить.

    Метка приходит БЕЗ ЗОНЫ (1С отдаёт местное время), и мы читаем её как UTC. Разница
    поясов попадает в то же смещение и вычитается вместе со сбоем часов — знать пояс
    сервера не требуется. Тем же вычитанием в `sort_time` метка приводится к нашим часам.
    """
    if not server_time:
        return None
    try:
        stamp = datetime.fromisoformat(server_time)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (stamp - (agent_now or _utcnow())).total_seconds()


class OnecProvider(Listener):
    """Провайдер и слушатель в одном лице.

    Слушатель — потому что снимок надо слать, лишь когда состояние менялось, а узнать об
    этом можно только от модели. Признак «менялось» ставит `notify`, снимает `collect`.
    """

    def __init__(self, onec, service, suppliers=None, sent=None) -> None:
        self._onec = onec
        self._service = service
        self._suppliers = suppliers
        # Хранилище связок «команда очереди → команда 1С» (`src/storage/sent_commands.py`).
        # Без него провайдер работает по-старому, в памяти: это годится для тестов, но не
        # для боевой работы — перезапуск процесса запирает форму навсегда.
        self._store = sent
        # Состояние считается изменившимся ИЗНАЧАЛЬНО: после подъёма процесса зеркало в 1С
        # могло остаться от прошлой жизни, и первый же оборот обязан его выровнять.
        self._dirty = True
        # Открыта ли форма 1С прямо сейчас. Читает `AgentLoop.period`: пока визуал жив,
        # цикл не уходит в тридцатисекундный простой, иначе первое нажатие после паузы
        # ждало бы до полуминуты. Поле ПУБЛИЧНОЕ намеренно — это ответ на вопрос снаружи,
        # а не внутреннее состояние провайдера.
        self.visual_active = False
        # id команды в очереди → идентификаторы команд 1С, которые в неё сложились.
        # СПИСОК, а не одно значение: две команды по одному объекту схлопываются в очереди
        # в одну строку, и завершать надо обе — иначе на форме навсегда останется гореть
        # колесико ожидания по команде, которой уже нет.
        self._sent: dict[int, list[str]] = {}
        # Карта поднимается из базы ОДИН раз, на первом обороте: дальше её ведёт сам
        # провайдер, и лишний SELECT каждые пять секунд ничего бы не добавил.
        self._restored = False
        # (инициатор, прайс, задача) → причина отказа. Заполняет `notify`, читает `collect`.
        self._refusals: dict[tuple, str] = {}
        # id команды в очереди → тот же ключ: событие отказа идентификатора не несёт, и
        # сопоставлять приходится по объекту.
        self._keys: dict[int, tuple] = {}
        self._names: dict[int, str] = {}

    # ------------------------------------------------------------------ слушатель

    async def notify(self, event: Event) -> None:
        self._dirty = True
        if event.kind == EventKind.COMMAND_REJECTED:
            key = (event.actor, event.price_id, event.task_id)
            self._refusals[key] = event.text

    # -------------------------------------------------------------------- оборот

    async def collect(self, queue) -> None:
        """Один оборот провайдера. Исключения наружу не выпускает.

        Цикл и так ловит их (`AgentLoop.tick`), но нам важно СОХРАНИТЬ признак «менялось»
        при сбое: иначе снимок, не доехавший из-за моргнувшей сети, не поехал бы никогда, и
        форма замерла бы на старом состоянии, ничем этого не показав.
        """
        # СНИМОК УХОДИТ ПЕРВЫМ, до отчёта о судьбе команд, и это не мелочь. Форма гасит
        # кнопки, пока команда не закрыта; закрыв её раньше снимка, мы на мгновение
        # отдали бы админу живые кнопки поверх СТАРЫХ данных — он увидел бы задачу
        # неизменившейся и решил бы, что нажатие пропало. В обратном порядке результат
        # появляется первым, а кнопки оживают уже поверх него.
        try:
            await self._push_state()
        except Exception:                               # noqa: BLE001
            logger.warning("Снимок состояния не доехал до 1С", exc_info=True)

        try:
            await self._finish_sent(queue)
        except Exception:                               # noqa: BLE001
            logger.warning("Не удалось подтвердить команды 1С", exc_info=True)

        try:
            await self._pull_commands(queue)
        except Exception:                               # noqa: BLE001
            logger.warning("Команды из 1С не забрались", exc_info=True)

    # ---------------------------------------------------------- судьба команд

    async def _finish_sent(self, queue) -> None:
        """Сообщить 1С исход команд, которых в очереди больше нет.

        «Нет в очереди» — единственный надёжный признак завершения: команду удаляет тот, кто
        её обработал, и это происходит и на успехе, и на отказе.

        **Связки поднимаются из базы**, иначе перезапуск процесса запирает форму: команда,
        взятая до остановки, после подъёма отрабатывается заново — но идентификатора
        команды 1С агент уже не знает, и колесико ожидания горит вечно (случай 24.09.2026).
        """
        await self._restore()
        if not self._sent:
            return

        alive = {c.id for c in await queue.pending()}
        alive |= {c.id for c in await queue.taken()}
        finished = [qid for qid in self._sent if qid not in alive]
        if not finished:
            return

        items = []
        for qid in finished:
            # Причина считается ОДИН РАЗ на команду очереди, а не на каждый внешний
            # идентификатор: схлопнутые команды — это одна работа с одним исходом, и
            # второе обращение вернуло бы пустоту, отчего одна из двух кнопок в форме
            # показала бы «выполнена» вместо отказа.
            reason = self._refusal_for(qid)
            state = "отклонена" if reason else "выполнена"
            for external in self._sent.get(qid, []):
                items.append({"id": external, "state": state, "message": reason})

        if items:
            # ЗАБЫВАЕМ ТОЛЬКО ПОСЛЕ УСПЕХА. Раньше карта чистилась до отправки, и
            # оборванный ответ уносил связку с собой — колесико оставалось гореть по
            # команде, исход которой мы знали. Исключение выпускает наружу `collect`,
            # который его ловит; связки остаются, и следующий оборот повторит попытку.
            await asyncio.to_thread(self._onec.agent_commands_state, items)

        for qid in finished:
            await self._forget(qid)

    async def _close_stale(self, stale) -> None:
        """Погасить команды, которые 1С считает взятыми, а агент о них не помнит.

        Своё — не трогаем: команда, чья связка ещё жива, сейчас в работе, и закрыть её
        значило бы погасить колесико раньше результата. Гасим только чужое прошлому
        процессу.
        """
        rows = [r for r in (stale or []) if isinstance(r, dict) and r.get("id")]
        if not rows:
            return

        mine = {x for ids in self._sent.values() for x in ids}
        items = [{"id": str(r["id"]), "state": "выполнена",
                  "message": "агент перезапускался, исход неизвестен — смотрите "
                             "результат задачи"}
                 for r in rows if str(r["id"]) not in mine]
        if not items:
            return

        logger.warning("Закрываю зависшие команды 1С: %d", len(items))
        await asyncio.to_thread(self._onec.agent_commands_state, items)

    async def _remember(self, queue_id: int, external: str) -> None:
        """Записать связку в базу. Сбой записи команду не роняет — она уже в очереди, и
        отказаться от неё сейчас значило бы потерять нажатие админа; в худшем случае
        форма останется запертой, как было до появления хранилища."""
        if self._store is None:
            return
        try:
            await self._store.remember(queue_id, external)
        except Exception:                               # noqa: BLE001
            logger.warning("Связка команды %s → %s не записалась", queue_id, external,
                           exc_info=True)

    async def _restore(self) -> None:
        """Поднять связки из базы. Один раз за жизнь процесса."""
        if self._restored:
            return
        self._restored = True
        if self._store is None:
            return
        try:
            saved = await self._store.all()
        except Exception:                               # noqa: BLE001
            logger.warning("Связки команд 1С не поднялись из базы", exc_info=True)
            self._restored = False                      # попробуем на следующем обороте
            return

        for qid, externals in saved.items():
            known = self._sent.setdefault(qid, [])
            for external in externals:
                if external not in known:
                    known.append(external)
        if saved:
            logger.info("Поднято связок команд 1С после перезапуска: %d", len(saved))

    async def _forget(self, queue_id: int) -> None:
        """Снять связку и причину отказа: исход доехал до формы."""
        self._sent.pop(queue_id, None)
        key = self._keys.pop(queue_id, None)
        if key is not None:
            self._refusals.pop(key, None)
        if self._store is not None:
            try:
                await self._store.forget(queue_id)
            except Exception:                           # noqa: BLE001
                logger.warning("Связка команды %s не снялась из базы", queue_id,
                               exc_info=True)

    def _refusal_for(self, queue_id: int) -> str:
        """Причина отказа по этой команде, если она была.

        Сопоставление идёт по (инициатор, прайс, задача), а не по идентификатору команды:
        события модели его не несут. Это ОДНОЗНАЧНО, и вот почему: очередь схлопывает
        команды по объекту, поэтому в работе одновременно не бывает двух команд по одной
        задаче от одного инициатора. Сломай кто-нибудь схлопывание — сломается и это.

        Причина ЧИТАЕТСЯ, а не снимается: снимет её `_forget`, когда исход доедет до
        формы. Иначе оборванный ответ превратил бы отказ в «выполнена» на повторе.

        У связки, поднятой из базы после перезапуска, причины нет — и это честно: событие
        отказа жило в памяти умершего процесса, а выдумывать текст нельзя. Команда
        закроется как выполненная, а что вышло на деле, видно в самой задаче: её результат
        приезжает снимком.
        """
        key = self._keys.get(queue_id)
        if key is None:
            return ""
        return self._refusals.get(key, "")

    # ------------------------------------------------------------------ снимок

    async def _push_state(self) -> None:
        if not self._dirty:
            return
        snapshot = await self.snapshot()
        # Признак снимаем ТОЛЬКО ПОСЛЕ успешной отправки: исключение оставит его поднятым,
        # и следующий оборот повторит попытку.
        await asyncio.to_thread(self._onec.set_model_state, snapshot,
                                await self.sheets_snapshot())
        self._dirty = False

    async def snapshot(self) -> list[dict]:
        """Полный снимок состояния модели в виде, который принимает `set-model-state`."""
        # Имена поставщиков перечитываются КАЖДЫЙ снимок: переименование происходит вне
        # модели, и кеш на весь процесс держал бы в форме имя, которого уже нет.
        self._names = {}
        out = []
        for price in self._service.prices:
            lock = self._service.lock_of(price.id)
            sp = price.supplier_price
            out.append({
                "id": price.id,
                "file": sp.filename,
                "supplier": await self._supplier_name(sp.supplier_id),
                # КОД, а не только имя: в 1С колонка «Поставщик» — ссылка на зеркало
                # справочника, и элемент там опознаётся по коду. По имени зеркало плодило
                # бы дубль на каждое переименование, унося с собой привязанные юрлица.
                "supplier_code": str(sp.supplier_id or ""),
                # СИГНАТУРА ФОРМАТА едет вместе с прайсом: по ней форма открывает выбор
                # листов двойным щелчком по имени файла. Иначе пришлось бы искать формат
                # по имени файла, а имена меняются каждый месяц.
                "signature": sp.signature or "",
                "status": price.status.value,
                "ready": price.ready,
                "has_newer": price.has_newer,
                "newer_id": price.newer_id,
                "locked_by": actor_label(lock.actor) if lock else "",
                "locked_until": lock.expires_at if lock else None,
                "created_at": price.created_at,
                "tasks": [self._task(task) for task in price.sorted_tasks],
            })
        return out

    async def sheets_snapshot(self) -> list[dict]:
        """Листы форматов с флажками — зеркало для формы выбора листов (02.10.2026).

        СТРОКА НА ЛИСТ, а не строка на формат со списком в поле: в форме это таблица с
        флажком, и 1С применяет снимок разницей — построчно. Список в одном поле означал бы
        перезапись всей таблицы на каждое изменение одного флажка.

        Порядок берём ИЗ ФАЙЛА, а не по алфавиту: админ ищет лист глазами там, где он стоит
        в книге, и «АКЦИИ» выше «ЛАМИНАТА» сбивало бы с толку.

        Без справочника (тесты, 1С без поставщиков) — пустой список: это честное «форматов
        не знаю», и зеркало честно опустеет.
        """
        if self._suppliers is None:
            return []

        out: list[dict] = []
        try:
            signatures = await self._suppliers.list_signatures()
        except Exception:                               # noqa: BLE001
            logger.warning("Не удалось прочитать форматы прайсов", exc_info=True)
            return []

        for sig in signatures:
            every = [n.strip() for n in (sig.sheet_list or "").split(",") if n.strip()]
            if not every:
                # Файла этого формата ещё не видели — показывать нечего, и пустая строка
                # в таблице только мешала бы.
                continue
            chosen = {n.strip().lower() for n in (sig.sheets or "").split(",") if n.strip()}
            supplier = await self._supplier_name(sig.supplier_id)
            for number, name in enumerate(every, 1):
                out.append({
                    "signature": sig.signature,
                    "supplier": supplier,
                    # КОД, а не только имя: в 1С колонка «Поставщик» — ССЫЛКА на зеркало
                    # справочника (правка админа 02.10.2026), и элемент там опознаётся по
                    # коду. По имени зеркало плодило бы дубль на каждое переименование.
                    "supplier_code": str(sig.supplier_id or ""),
                    "format": sig.purpose or sig.sample_name or "",
                    "sheet": name,
                    "order": number,
                    "parse": name.strip().lower() in chosen,
                })
        return out

    def _task(self, task) -> dict:
        return {
            "id": task.id,
            "kind": task.kind.value,
            # `Порядок` в 1С считается от единицы, а `kind.order` — от нуля. Номер нужен
            # именно числом: порядок видов задан спекой и не алфавитный, и знать его в 1С
            # незачем — пусть сортирует по присланному.
            "order": task.kind.order + 1,
            "subject": task.subject.value,
            "address": task.address.label(),
            "status": task.status.value,
            "description": task.description,
            "result": task.result,
            # Отметка о ПРОГОНЕ. В 1С ложится в колонку «Выполнена» — отдельного поля под
            # неё не заводили: у закрытой задачи это один и тот же прогон, а у незакрытой
            # прежняя дата всё равно была пустой.
            "run_at": task.run_at,
        }

    async def _supplier_name(self, supplier_id: int) -> str:
        """Имя поставщика для колонки «Поставщик».

        **Кеш живёт ОДИН СНИМОК, а не весь процесс.** Админ переименовал поставщика
        командой в Telegram, форма обновилась — и показала старое имя: провайдер помнил
        его с первого оборота и в справочник больше не заглядывал (бой 22.09.2026).
        Снимки редки, поставщиков десятки, запрос идёт в локальный SQLite — экономить тут
        было не на чем.
        """
        if supplier_id in self._names:
            return self._names[supplier_id]
        name = ""
        if self._suppliers is not None:
            try:
                supplier = await self._suppliers.get_supplier(supplier_id)
                name = supplier.name if supplier else ""
            except Exception:                           # noqa: BLE001
                logger.warning("Имя поставщика %s не прочиталось", supplier_id,
                               exc_info=True)
                return ""                               # не кешируем неудачу
        self._names[supplier_id] = name
        return name

    # ------------------------------------------------------------------ команды

    async def _pull_commands(self, queue) -> None:
        answer = await asyncio.to_thread(self._onec.agent_commands)
        commands = answer.get("commands") or []

        # ОТКРЫТА ЛИ ФОРМА. Снимается ДО всех ранних выходов: признак нужен циклу на
        # каждом обороте, в том числе на пустом — именно пустые обороты и решают, засыпать
        # ли до тридцати секунд. Старый BSL поля не отдаёт, и тогда это просто «нет».
        self.visual_active = bool(answer.get("visual_active"))

        # ЗАСТРЯВШИЕ «ПРИНЯТЫЕ» ГАСИМ ПЕРВЫМ ДЕЛОМ, до разбора ждущих.
        #
        # Такая команда запирает форму навсегда: 1С считает её живой, а прежняя выдача
        # отдавала только «ждёт» — агент её больше не видел и закрыть не мог. Дважды за
        # трое суток это лечилось удалением строки регистра руками (28.09.2026).
        #
        # ВЫПОЛНЯТЬ ЗАНОВО НЕЛЬЗЯ: неизвестно, применилась ли она в прошлый раз, а
        # «перенести в снятые» дважды — это перенести чужое. Поэтому только закрываем,
        # честно говоря админу, что исход неизвестен: что вышло на самом деле, видно в
        # самой задаче — её результат приезжает снимком.
        await self._close_stale(answer.get("stale"))

        missing = answer.get("missing")
        if missing:
            logger.error("В 1С не хватает объектов конфигурации: %s", ", ".join(missing))

        if answer.get("error"):
            logger.error("Эндпоинт команд 1С ответил ошибкой: %s", answer["error"])

        if not commands:
            return

        offset = clock_offset(answer.get("server_time"))
        # Смещение не измерилось — метки не приводятся, и `sort_time` подставит наше время
        # (`trust=False`). Это честнее, чем поверить неизвестным часам: неверный порядок
        # тише и хуже, чем потерянный.
        agent_now = _utcnow()

        taken, refused = [], []
        for raw in commands:
            external = str(raw.get("id") or "")
            if not external:
                continue
            parsed = self._parse(raw)
            if parsed is None:
                refused.append({"id": external, "state": "отклонена",
                                "message": f"неизвестный вид команды: {raw.get('kind')}"})
                continue

            stored = await queue.put(parsed, offset or 0.0, agent_now)
            known = self._sent.setdefault(stored.id, [])
            if external not in known:
                known.append(external)
            self._keys[stored.id] = (parsed.actor, parsed.price_id, parsed.task_id)
            # СВЯЗКА ЛОЖИТСЯ В БАЗУ ДО того, как в 1С уедет «принята»: пока команда там
            # «ждёт», она приедет снова и схлопнётся с уже лежащей в очереди — окно
            # самоизлечимо. После «принята» 1С её больше не отдаст, и потерянная связка
            # заперла бы форму насовсем.
            await self._remember(stored.id, external)
            taken.append({"id": external, "state": "принята", "message": ""})

        if taken or refused:
            await asyncio.to_thread(self._onec.agent_commands_state, taken + refused)

    def _parse(self, raw: dict) -> Command | None:
        """Команду 1С — во внутреннюю. None, если вид неизвестен.

        Неизвестный вид не роняет пачку и не остаётся висеть: он отклоняется с внятным
        текстом, иначе на форме навсегда горело бы колесико ожидания.
        """
        try:
            kind = CommandKind(str(raw.get("kind") or "").strip())
        except ValueError:
            return None

        price_id = raw.get("price_id")
        task_id = raw.get("task_id")
        return Command(
            kind=kind,
            source=SOURCE,
            actor=actor_of(str(raw.get("actor") or "")),
            # Ноль в 1С означает «команда про прайс целиком»: у регистра числовой ресурс, и
            # пустого значения в нём не бывает.
            price_id=int(price_id) if price_id else None,
            task_id=int(task_id) if task_id else None,
            payload=dict(raw.get("payload") or {}),
            # Пустая метка допустима: `sort_time` подставит наше время и скажет об этом
            # в журнале. Ронять команду из-за отсутствующей метки нельзя — админ её нажал.
            created_at=str(raw.get("created_at") or "") or commands_now(),
        )
