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
from src.bot.model_handlers import (TelegramListener, TelegramProvider,
                                    router as model_router)
from src.bot.model_loop import AgentLoop
from src.bot.pricing_handlers import router as pricing_router
from src.config import load_config
from src.email_tool.client import MailClient
from src.onec.client import OnecClient
from src.onec.model_provider import OnecProvider
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
    known = (await pricing_store.known_price_paths()
             | await supplier_store.known_paths()
             | await model_store.known_paths())
    price_files.sweep(config.db_path, known)

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
        executor=ToolExecutor(mail, norwik, onec=onec, pricing_store=pricing_store),
        # Учёт расхода токенов (§9.6.3): журнал хранилища и есть приёмник. Метки к строке
        # добавляют обработчики — только они знают, чей это вызов и по какому прайсу.
        on_usage=pricing_store.record_usage,
    )

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

        # Ответ агента едет дальше вместе с задачами: когда их ноль, только он и
        # объясняет, почему — «расхождений нет» или «разобрал не тот лист».
        return await build(orchestrator, content, filename, onec=onec,
                           usage_labels={"kind": "pricing", "price_doc": filename},
                           elsewhere=await sightings.elsewhere(supplier_id),
                           remember=remember)

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
        return await run(orchestrator, onec, price, task, content, guard, scope=scope,
                         usage_labels={"kind": "model_task",
                                       "price_doc": price.supplier_price.filename},
                         offers=sightings,
                         supplier_name=seller.name if seller else "")

    model = PriceListService(
        model_store, supplier_store,
        save_file=lambda content, name: price_files.save(config.db_path, name, content),
        build_tasks=build_tasks,
        # БЕЗ 1С ВЫПОЛНЕНИЕ ОСТАЁТСЯ ЗАГЛУШКОЙ. Дать агенту инструменты записи, которым
        # некуда писать, значит получить прогон, честно доложивший об успехе на ошибках
        # соединения.
        run_task=run_task if onec is not None else None)
    await model.load()
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

    dp = Dispatcher(store=store, orchestrator=orchestrator, openai_api_key=config.openai_api_key,
                    onec=onec, pricing_store=pricing_store,
                    supplier_store=supplier_store, model=model, queue=commands, loop=loop)
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
    await asyncio.gather(dp.start_polling(bot), loop.run())


if __name__ == "__main__":
    asyncio.run(main())
