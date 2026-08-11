"""Загрузка конфигурации из .env с проверкой обязательных переменных."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REQUIRED = [
    "TELEGRAM_BOT_TOKEN",
    "ADMIN_TELEGRAM_ID",
    "MAIL_USER",
    "MAIL_PASSWORD",
    "ANTHROPIC_API_KEY",
]


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    admin_telegram_id: int
    mail_host: str
    mail_port: int
    mail_user: str
    mail_password: str
    anthropic_api_key: str
    db_path: Path
    openai_api_key: str | None
    onec_base_url: str | None
    onec_token: str | None
    price_min_change_pct: float


def load_config() -> Config:
    missing = [name for name in _REQUIRED if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            f"Отсутствуют обязательные переменные окружения: {', '.join(missing)}. "
            f"Скопируйте .env.example в .env и заполните значения."
        )
    return Config(
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        admin_telegram_id=int(os.environ["ADMIN_TELEGRAM_ID"]),
        mail_host=os.getenv("MAIL_HOST", "imap.mail.ru"),
        mail_port=int(os.getenv("MAIL_PORT", "993")),
        mail_user=os.environ["MAIL_USER"],
        mail_password=os.environ["MAIL_PASSWORD"],
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        db_path=Path(os.getenv("DB_PATH", "data/users.db")),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        onec_base_url=(os.getenv("ONEC_BASE_URL") or "").rstrip("/") or None,
        onec_token=os.getenv("ONEC_TOKEN") or None,
        # порог значимости изменения цены, % (§9.1 спеки): меньше — не пишем
        price_min_change_pct=float(os.getenv("PRICE_MIN_CHANGE_PCT") or 2.0),
    )
