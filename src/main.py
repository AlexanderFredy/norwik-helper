"""Точка входа: запуск Telegram-бота."""
import asyncio
import logging

from aiogram import Bot, Dispatcher

from src.agent.orchestrator import Orchestrator
from src.agent.tools import ToolExecutor
from src.bot.auth import AuthMiddleware
from src.bot.handlers import router
from src.config import load_config
from src.email_tool.client import MailClient
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

    mail = MailClient(
        config.mail_host, config.mail_port, config.mail_user, config.mail_password
    )
    norwik = NorwikClient()
    orchestrator = Orchestrator(
        api_key=config.anthropic_api_key,
        executor=ToolExecutor(mail, norwik),
    )

    bot = Bot(token=config.telegram_bot_token)
    dp = Dispatcher(store=store, orchestrator=orchestrator)
    dp.message.middleware(AuthMiddleware(store, config.admin_telegram_id))
    dp.include_router(router)

    logger.info("Запуск бота (polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
