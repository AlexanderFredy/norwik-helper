"""Точка входа: запуск Telegram-бота."""
import asyncio
import logging

from aiogram import Bot, Dispatcher

from src.agent.orchestrator import Orchestrator
from src.agent.tools import ToolExecutor
from src.bot.auth import AuthMiddleware
from src.bot.commands import setup_bot_commands
from src.bot.handlers import router
from src.bot.pricing_handlers import router as pricing_router
from src.config import load_config
from src.email_tool.client import MailClient
from src.onec.client import OnecClient
from src.storage.pricing import PricingStore
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
    )

    bot = Bot(token=config.telegram_bot_token)
    dp = Dispatcher(store=store, orchestrator=orchestrator, openai_api_key=config.openai_api_key,
                    onec=onec, pricing_store=pricing_store)
    dp.message.middleware(AuthMiddleware(store, config.admin_telegram_id))
    dp.callback_query.middleware(AuthMiddleware(store, config.admin_telegram_id))
    dp.include_router(pricing_router)   # прайсы — до общего роутера: он ловит любой текст
    dp.include_router(router)

    await setup_bot_commands(bot, config.admin_telegram_id)

    logger.info("Запуск бота (polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
