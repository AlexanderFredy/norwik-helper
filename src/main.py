"""Точка входа: запуск Telegram-бота."""
import asyncio
import logging

from aiogram import Bot, Dispatcher

from src.bot.auth import AuthMiddleware
from src.bot.handlers import router
from src.config import load_config
from src.storage.users import UserStore

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

    bot = Bot(token=config.telegram_bot_token)
    dp = Dispatcher(store=store)
    dp.message.middleware(AuthMiddleware(store, config.admin_telegram_id))
    dp.include_router(router)

    logger.info("Запуск бота (polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
