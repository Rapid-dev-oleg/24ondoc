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

    async def update_twenty_member_id(
        self, telegram_id: int, twenty_member_id: str
    ) -> UserProfile | None:
        profile = await self._repo.get_by_telegram_id(telegram_id)
        if profile is None:
            return None
        profile.twenty_member_id = twenty_member_id
        await self._repo.save(profile)
        return profile

    async def upsert_user(
        self,
        telegram_id: int,
        twenty_member_id: str,
        role: str,
        display_name: str = "",
    ) -> UserProfile:
        from ..domain.models import UserRole

        profile = await self._repo.get_by_telegram_id(telegram_id)
        if profile is None:
            profile = UserProfile(
                telegram_id=telegram_id,
                twenty_member_id=twenty_member_id,
                role=UserRole(role),
                settings={"display_name": display_name} if display_name else {},
            )
        else:
            profile.twenty_member_id = twenty_member_id
            profile.role = UserRole(role)
            if display_name:
                profile.settings["display_name"] = display_name
            profile.is_active = True
        await self._repo.save(profile)
        return profile
