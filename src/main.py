"""Точка входа: запуск Telegram-бота."""
import asyncio
import logging

from src.config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    config = load_config()
    logger.info("Конфигурация загружена, ящик: %s", config.mail_user)
    # Фаза 1: здесь будет запуск aiogram-бота
    logger.info("Каркас готов. Бот будет добавлен в Фазе 1.")


if __name__ == "__main__":
    asyncio.run(main())
