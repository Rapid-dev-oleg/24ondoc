"""Telegram Ingestion — UserProfilePort adapter over UserProfileRepository."""

from __future__ import annotations

from ..application.ports import UserProfilePort
from ..domain.models import UserProfile
from ..domain.repository import UserProfileRepository


class UserProfilePortAdapter(UserProfilePort):
    """Adapts UserProfileRepository to UserProfilePort interface."""

    def __init__(self, repo: UserProfileRepository) -> None:
        self._repo = repo

    async def is_authorized(self, telegram_id: int) -> bool:
        profile = await self._repo.get_by_telegram_id(telegram_id)
        return profile is not None

    async def get_profile(self, telegram_id: int) -> UserProfile | None:
        return await self._repo.get_by_telegram_id(telegram_id)

    async def list_active_agents(self) -> list[UserProfile]:
        return await self._repo.list_active()
