"""Middleware: пропускает только админа и пользователей из whitelist."""
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message

from src.storage.users import UserStore

ACCESS_DENIED = "Доступ запрещён"


class AuthMiddleware(BaseMiddleware):
    def __init__(self, store: UserStore, admin_id: int) -> None:
        self._store = store
        self._admin_id = admin_id

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        user = event.from_user
        if user is None:
            return None
        if user.id == self._admin_id or await self._store.is_allowed(user.id):
            data["is_admin"] = user.id == self._admin_id
            return await handler(event, data)
        await event.answer(ACCESS_DENIED)
        return None
