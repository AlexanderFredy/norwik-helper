"""Точка входа: запуск Telegram-бота."""
import asyncio
import logging

from aiogram import Bot, Dispatcher

from src.agent.orchestrator import Orchestrator
from src.agent.tools import ToolExecutor
from src.bot import pricing_handlers
from src.storage import price_files
from src.bot.auth import AuthMiddleware
from src.bot.commands import setup_bot_commands
from src.bot.handlers import router
from src.bot.catalog_handlers import router as catalog_router
from src.bot.model_handlers import (ManagerListener, TelegramListener,
                                    TelegramProvider,
                                    router as model_router)
from src.bot.model_loop import AgentLoop
from src.bot import photo_daily
from src.bot.polling import poll_forever
from src.bot.pricing_handlers import router as pricing_router
from src.config import load_config
from src.email_tool.client import MailClient
from src.onec.client import OnecClient
from src.onec.model_provider import OnecProvider
from src.storage.photo_subscribers import PhotoSubscriberStore
from src.storage.photo_watch import PhotoWatchStore
from src.storage.pricing import PricingStore
from src.model.service import PriceListService
from src.storage.command_queue import CommandQueue
from src.storage.model_store import ModelStore
from src.storage.sent_commands import SentCommands
from src.storage.sightings import SightingStore
from src.storage.suppliers import SupplierStore
from src.storage.users import UserStore
from src.website_tool.norwik import NorwikClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    config = load_config()
    logger.info("Конфигурация загружена, ящик: %s", config.mail_user)

    store = UserStore(config.db_path)
    await store.init()
    pricing_store = PricingStore(config.db_path)
    await pricing_store.init()
    # Справочники поставщиков (§2 spec/agent-workflow-model.md). Та же база: поставщик —
    # сквозная сущность, и второй файл развёл бы её по двум местам.
    supplier_store = SupplierStore(config.db_path)
    await supplier_store.init()
    # Состояние модели работы с прайсами (§10). Пока только хранилище: список прайсов и
    # задач поднимается в память, но команд, которые его меняют, ещё нет.
    model_store = ModelStore(config.db_path)
    await model_store.init()
    # Журнал встреч артикулов в прайсах (§6.1): по нему коллекция, которую возит другой
    # поставщик, не уезжает в снятые. Наполняется кодом, токенов не стоит.
    sightings = SightingStore(config.db_path)
    await sightings.init()

    # Очередь команд визуалов (§7). Взятая, но не завершённая команда означает одно:
    # процесс умер, не доработав. Живых взятых в момент старта быть не может — агент
    # один, — поэтому возвращаем их в очередь, а не гадаем, кто их держит.
    commands = CommandQueue(config.db_path)
    await commands.init()
    stale = await commands.requeue_stale()
    if stale:
        logger.info("Возвращено в очередь команд после перезапуска: %d", stale)


    # Прайсы, которые админы разбирали до перезапуска, поднимаем обратно в память: история
    # диалога лежит в базе и рестарт переживает, а файл до 10.09.2026 не переживал — и
    # текст админа переставал считаться ответом по прайсу (§9.7).
    restored = await pricing_handlers.restore_active_prices(pricing_store)
    if restored:
        logger.info("Возобновлены прайсы в работе: %d", restored)

    # Файл пишется на диск раньше строки в базе, и падение между этими шагами оставляет
    # сироту. Чистим ТОЛЬКО здесь: пока бот работает, файл может быть создан секунду назад
    # и ещё не попасть в базу — гонка, в которой мы стёрли бы нужное (§9.8).
    #
    # Ссылки собираем ИЗ ВСЕХ ТРЁХ хранилищ. Забыть здесь одно — значит стереть файл,
    # на который оно ссылается: для модели это прайс без файла, а такого объекта
    # существовать не может (§3.1 agent-workflow-model.md).
    # ПУТИ — К ПРЯМЫМ СЛЭШАМ ДО УБОРКИ: база переезжает с Windows на Linux, и путь
    # «data\prices\x.xlsx» там не указывает ни на что (`storage/path_fix.py`).
    from src.storage import path_fix
    await path_fix.normalize(config.db_path)

    known = (await pricing_store.known_price_paths()
             | await supplier_store.known_paths()
             | await model_store.known_paths())
    price_files.sweep(config.db_path, known)

    # ХЕШИ ФОРМАТОВ ПЕРЕСЧИТЫВАЕМ ПО НОВОМУ ПРАВИЛУ. Скелет перестал тащить в хеш данные
    # файла (02.10.2026), и старые значения осиротели бы молча: следующий прайс каждого
    # поставщика не нашёл бы владельца формата и завёл бы второго поставщика по имени файла,
    # обнулив выбор листов. Идемпотентно — на втором старте работы нет.
    from src.model.signature_rehash import rehash_signatures

    rehashed = await rehash_signatures(supplier_store, pricing=pricing_store,
                                       sightings=sightings, model_store=model_store)
    for edit in rehashed:
        logger.info("Хеш формата пересчитан: %s %s → %s (по файлу %s)",
                    edit["supplier"], edit["old"][:12], edit["new"][:12], edit["file"])

    # ЛИСТЫ ФОРМАТОВ, заведённых до появления этой памяти, дозаполняем из файлов на диске:
    # иначе форма выбора листов открывается у них пустой таблицей, и видно только то, что
    # выбирать не из чего, — а почему, не видно (бой 02.10.2026).
    from src.model.sheet_backfill import fill_sheet_lists

    filled = await fill_sheet_lists(supplier_store)
    if filled:
        logger.info("Листы форматов дозаполнены: %d", filled)

    # Журнал наблюдений за фото: по нему считается прогресс, а не снимок «48 без фото».
    photo_watch = PhotoWatchStore(config.db_path)
    await photo_watch.init()
    # Кому уходит еженедельный дайджест. Пустой список значит «никому», в том числе
    # администратору: доступ к боту и подписка на рассылку — разные вещи.
    photo_subscribers = PhotoSubscriberStore(config.db_path)
    await photo_subscribers.init()

    onec = None
    if config.onec_base_url and config.onec_token:
        onec = OnecClient(config.onec_base_url, config.onec_token, timeout=120)
        logger.info("Интеграция с 1С включена: %s", config.onec_base_url)
    else:
        logger.warning("ONEC_BASE_URL/ONEC_TOKEN не заданы — обновление цен недоступно")

    mail = MailClient(
        config.mail_host, config.mail_port, config.mail_user, config.mail_password
    )
    norwik = NorwikClient()
    orchestrator = Orchestrator(
        api_key=config.anthropic_api_key,
        model=config.anthropic_model,
        executor=ToolExecutor(mail, norwik, onec=onec, pricing_store=pricing_store,
                              photo_watch=photo_watch),
        # Учёт расхода токенов (§9.6.3): журнал хранилища и есть приёмник. Метки к строке
        # добавляют обработчики — только они знают, чей это вызов и по какому прайсу.
        on_usage=pricing_store.record_usage,
    )

    # БРЕНДЫ ФОРМАТОВ дозаполняем из файлов на диске — как листы, и по той же причине:
    # собираются они на приёме прайса, и у форматов, заведённых раньше, список пуст, а форма
    # открывается с пустой таблицей. Стоит это ПОСЛЕ оркестратора и клиента 1С: марку кодом
    # лишь ПРЕДЛАГАЕМ (справочник марок в 1С), а бренд-картинку читает модель. Нет ни того,
    # ни другого — список всё равно соберётся, просто без предложенных марок и без логотипов.
    from src.model.brand_backfill import fill_brand_lists
    from src.model.logo_intake import name_logos

    try:
        known_marks = await asyncio.to_thread(onec.selling_tm) if onec else []
    except Exception:                                   # noqa: BLE001
        logger.warning("Справочник марок не прочитался — бренды дозаполним без марок",
                       exc_info=True)
        known_marks = []

    async def read_logos(signature, content, filename):
        return await name_logos(orchestrator, supplier_store, signature, content, filename)

    with_brands = await fill_brand_lists(supplier_store, known_marks, read_logos)
    if with_brands:
        logger.info("Бренды форматов дозаполнены: %d", with_brands)

    async def build_tasks(content, filename, price):
        """Список задач составляет агент (§6.1). Метки расхода — как у прайсового
        прогона: по ним видно, во что обходится формирование (§9.6.3)."""
        from src.model.task_builder import build

        # ЖУРНАЛ ВСТРЕЧ (§6.1): что этот прайс показал — туда, что показали ЧУЖИЕ прайсы
        # — оттуда. Коллекция, которую возит другой поставщик, не снимается с производства.
        supplier_id = price.supplier_price.supplier_id
        supplier = await supplier_store.get_supplier(supplier_id)
        signature = price.supplier_price.signature or ""

        async def remember(articles, prices=None):
            await sightings.remember(
                supplier_id, signature, articles,
                supplier=supplier.name if supplier else "",
                price_date=price.supplier_price.price_date,
                prices=prices)

        # ТРАКТОВКА КОЛОНОК ЖИВЁТ ПО СИГНАТУРЕ формата, а не по файлу: выбор между
        # двумя колонками закупки («самовывоз» и «с доставкой») — договорённость с
        # поставщиком, и переспрашивать её на каждом прайсе незачем.
        known_columns = {row.get("sheet", ""): (row.get("mapping") or {})
                         for row in await pricing_store.get_mappings(signature)}

        # КАКИЕ ЛИСТЫ РАЗБИРАТЬ — указание админа, привязанное к СИГНАТУРЕ формата
        # (`/signature_sheets`). У FLOOR SERVICE четырнадцать листов, по делу два-три, и
        # каждый лишний стоит и токенов, и кругов цикла.
        only_sheets = await supplier_store.sheets_for(signature)

        # БРЕНДЫ ВНУТРИ ЛИСТА — второе указание админа (решение 03.10.2026). Выбор листов
        # не помогает прайсу, у которого лист один: у «Остатков» одна вкладка на 1286 строк
        # и 23 бренда, 88 тыс. токенов за полный разбор, и 13 брендов из 23 в справочнике
        # 1С отсутствуют вовсе. К разбору уходит ПЕРЕСЕЧЕНИЕ отмеченного.
        #
        # Состав брендов пересобираем ЗДЕСЬ ЖЕ, на каждом разборе: только здесь есть и файл,
        # и справочник марок 1С для подсказки. Решение админа при этом не затирается —
        # `remember_marks` хранит флажки, марки и скидки, а новые бренды приезжают
        # неотмеченными.
        from src.model.brand_intake import remember as remember_brands
        from src.model.logo_intake import name_logos

        # БРЕНД БЫВАЕТ КАРТИНКОЙ (решение админа 04.10.2026). Код знает, в какой СТРОКЕ
        # лежит баннер, а имя на нём читает модель — один раз на логотип, дальше по хешу
        # картинки из памяти, то есть следующий файл того же поставщика бесплатен. Читаем
        # ДО состава брендов: список и фильтр обязаны видеть одну и ту же разметку.
        logos = {}
        try:
            logos = await name_logos(orchestrator, supplier_store, signature,
                                     content, filename)
        except Exception:                               # noqa: BLE001
            logger.warning("Логотипы прайса %s не прочитаны", filename, exc_info=True)

        only_marks = None
        marks = []
        try:
            marks = await asyncio.to_thread(onec.selling_tm) if onec else []
            summary = await remember_brands(supplier_store, signature, content,
                                            filename, marks, logos)
            if summary["brands"]:
                only_marks = await supplier_store.marks_wanted(signature)
        except Exception:                               # noqa: BLE001
            # Бренды не собрались — это не повод не собирать задачи: работаем как прежде,
            # по листам. Молча этого не оставляем, но и прогон не роняем.
            logger.warning("Бренды формата %s не собраны", signature[:12], exc_info=True)

        # УСЛОВИЯ ПЕРЕСЧЁТА ЦЕН для прайсов, где закупки нет (решение админа 03.10.2026):
        # скидка дилера от розницы — своя у каждого бренда, курс валюты — у прайса. Формулу
        # подтвердил админ: закупка = розница × (1 − скидка/100), РРЦ = розница.
        discounts = {}
        try:
            discounts = {m.brand: m.discount
                         for m in await supplier_store.marks_for(signature)
                         if m.discount}
        except Exception:                               # noqa: BLE001
            logger.warning("Скидки формата %s не прочитаны", signature[:12], exc_info=True)

        async def note(text):
            """Что разобрали и что пропустили — сообщением админу. Отдельно от задач:
            по их списку не видно, обошли прайс целиком или треть его."""
            from src.model.events import Event, EventKind

            await model.events.publish(Event(
                EventKind.TASKS_REBUILT, price_id=price.id, text=text))

        async def remember_columns(by_sheet):
            for sheet, spec in (by_sheet or {}).items():
                await pricing_store.save_mapping(
                    signature, supplier.name if supplier else "", spec, sheet)

        # Ответ агента едет дальше вместе с задачами: когда их ноль, только он и
        # объясняет, почему — «расхождений нет» или «разобрал не тот лист».
        # КАТЕГОРИИ (`/categories`) нужны и ЗДЕСЬ, не только исполнителю: раздел чужого
        # вида товара — это не работа, и задачу по нему заводить незачем. Решает код.
        return await build(orchestrator, content, filename, onec=onec,
                           usage_labels={"kind": "pricing", "price_doc": filename},
                           elsewhere=await sightings.elsewhere(supplier_id),
                           # Цены других поставщиков: сверка цен идёт с ПОБЕДИТЕЛЕМ, как и
                           # запись, иначе задача заводилась бы там, где выиграет чужой прайс.
                           rivals=await sightings.rival_offers(supplier_id),
                           price_date=price.supplier_price.price_date,
                           remember=remember,
                           scope=[c["category"] for c in await pricing_store.list_scope()],
                           known_columns=known_columns,
                           remember_columns=remember_columns,
                           only_sheets=only_sheets, only_marks=only_marks,
                           discounts=discounts, logos=logos, catalogue=marks,
                           currency={"code": price.supplier_price.currency_code,
                                     "name": price.supplier_price.currency_name,
                                     "rate": price.supplier_price.rate},
                           note=note)

    async def run_task(price, task, content, guard):
        """Выполнение задачи агентом (§6.2) — С НАСТОЯЩЕЙ ЗАПИСЬЮ в 1С.

        `guard` приходит из модели и бросает, если прогон потерял право писать;
        исполнитель зовёт его вплотную перед каждой записью.

        Категории (`/categories`) передаются сюда по той же причине, что и в прайсовый
        поток: они ограничивают, что вообще разрешено трогать, и проверяются кодом, а не
        моделью.
        """
        from src.model.executor import run
        scope = [c["category"] for c in await pricing_store.list_scope()]
        # Имя поставщика едет в 1С вместе с ценой: там заводится зеркало справочника,
        # опознаваемое по КОДУ, а имя — для глаз (§6.4).
        seller = await supplier_store.get_supplier(price.supplier_price.supplier_id)
        # ЖУРНАЛ ПРЕДЛОЖЕНИЙ (§6.4): по нему исполнитель пишет наименьшую АКТУАЛЬНУЮ цену,
        # а не ту, что в обрабатываемом прайсе. Без журнала поведение прежнее.
        # УСЛОВИЯ ПЕРЕСЧЁТА ЦЕН для прайса без закупки (решение админа 03.10.2026).
        # Скидка задана у БРЕНДА, задача адресована МАРКЕ — связывает их `tm_code`,
        # который админ выставил в форме брендов. Не нашли — условий нет, и цены по такой
        # задаче не запишутся вовсе: лучше отказ с причиной, чем цифра наугад в боевой 1С.
        terms = None
        try:
            from src.model.dealer_price import Terms

            code = (task.address.tm.code or "").strip()
            marks = await supplier_store.marks_for(price.supplier_price.signature or "")
            mine = next((m for m in marks if m.tm_code and m.tm_code == code), None)
            if mine is not None:
                terms = Terms(discount=mine.discount,
                              rate=price.supplier_price.rate,
                              currency=price.supplier_price.currency_code,
                              currency_name=price.supplier_price.currency_name)
        except Exception:                               # noqa: BLE001
            logger.warning("Условия пересчёта цен не прочитаны", exc_info=True)

        return await run(orchestrator, onec, price, task, content, guard, scope=scope,
                         usage_labels={"kind": "model_task",
                                       "price_doc": price.supplier_price.filename},
                         offers=sightings,
                         supplier_name=seller.name if seller else "",
                         terms=terms)

    model = PriceListService(
        model_store, supplier_store,
        save_file=lambda content, name: price_files.save(config.db_path, name, content),
        build_tasks=build_tasks,
        # БЕЗ 1С ВЫПОЛНЕНИЕ ОСТАЁТСЯ ЗАГЛУШКОЙ. Дать агенту инструменты записи, которым
        # некуда писать, значит получить прогон, честно доложивший об успехе на ошибках
        # соединения.
        run_task=run_task if onec is not None else None)
    # ДАТЫ ПРАЙСОВ — ДО ЗАГРУЗКИ МОДЕЛИ: она читает их из базы один раз. Без даты цены
    # прайса в выборе наименьшей между поставщиками не стареют никогда.
    from src.model.price_dates import backfill as backfill_price_dates
    try:
        await backfill_price_dates(model_store, sightings)
    except Exception:                                   # noqa: BLE001
        logger.warning("Даты прайсов не дозаполнены", exc_info=True)
    await model.load()
    # Модель подключается к инструментам ПОСЛЕ создания: она строится с обработчиками,
    # которые сами зовут оркестратор (сборка задач, выполнение), и раньше него появиться
    # не может. Без этой строки агент на вопрос «в этом прайсе» отвечает, что прайса у
    # него нет, и предлагает поискать письмо в почте.
    orchestrator.executor.use_model(model)
    logger.info("Модель поднята: прайсов %d", len(model.prices))

    bot = Bot(token=config.telegram_bot_token)
    # ВТОРОЙ ВИЗУАЛ — форма 1С (specs/1c-model-form.md). Подключается, только если 1С
    # настроена: без неё провайдер на каждом обороте ходил бы в никуда.
    #
    # Провайдер он же слушатель: снимок состояния уезжает в 1С, лишь когда состояние
    # менялось, а узнать об этом можно только от модели. Подписка ОБЯЗАТЕЛЬНА — без неё
    # зеркало выровняется один раз при подъёме и застынет.
    providers = [TelegramProvider()]
    if onec is not None:
        # Связки «команда очереди → команда 1С» хранятся в БАЗЕ: перезапуск процесса
        # между «принята» и исходом иначе запирает форму навсегда (24.09.2026).
        sent_commands = SentCommands(config.db_path)
        await sent_commands.init()
        onec_provider = OnecProvider(onec, model, supplier_store, sent=sent_commands)
        providers.append(onec_provider)
        model.events.subscribe(onec_provider)
        logger.info("Форма 1С подключена как второй визуал")

    loop = AgentLoop(commands, model, providers=providers)
    model.events.subscribe(TelegramListener(bot, [config.admin_telegram_id]))
    # СВОДКА ПО ПРАЙСУ — ЕДИНСТВЕННОЕ, что уходит НЕ админу: менеджерам из белого
    # списка, когда админ закрывает прайс и подтверждает это (§ решение 28.09.2026).
    model.events.subscribe(ManagerListener(bot, store, config.admin_telegram_id))

    dp = Dispatcher(store=store, orchestrator=orchestrator, openai_api_key=config.openai_api_key,
                    onec=onec, pricing_store=pricing_store,
                    supplier_store=supplier_store, model=model, queue=commands, loop=loop,
                    photo_subscribers=photo_subscribers, photo_watch=photo_watch)
    dp.message.middleware(AuthMiddleware(store, config.admin_telegram_id))
    dp.callback_query.middleware(AuthMiddleware(store, config.admin_telegram_id))
    dp.include_router(catalog_router)   # справочники: только команды, конфликтов нет
    # СТАРЫЙ ПРАЙСОВЫЙ ПОТОК ОТКЛЮЧЁН на время обкатки модели: оба реагировали бы на один
    # присланный файл. Вернуть — раскомментировать строку ниже.
    # dp.include_router(pricing_router)
    dp.include_router(model_router)     # модель: команды и приём файла
    dp.include_router(router)

    await setup_bot_commands(bot, config.admin_telegram_id)

    logger.info("Запуск бота (polling)")
    # Поллинг поднимается через `poll_forever`, а не напрямую: моргнувший на старте DNS
    # ронял процесс насовсем, хотя ждать надо было секунды, — и уносил с собой цикл
    # модели, которому Telegram вообще не нужен (у формы 1С свой канал).
    jobs = [poll_forever(dp, bot), loop.run()]
    if onec is not None:
        # Ежедневная проверка фото наполняет журнал, по понедельникам шлёт админу
        # напоминание о просроченных. Без 1С брать список новых товаров неоткуда.
        jobs.append(photo_daily.run_forever(onec, photo_watch, bot,
                                            photo_subscribers))
    await asyncio.gather(*jobs)


if __name__ == "__main__":
    asyncio.run(main())
