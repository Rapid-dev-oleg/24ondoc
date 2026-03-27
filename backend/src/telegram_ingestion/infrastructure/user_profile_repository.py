"""Telegram Ingestion — SQLAlchemy UserProfileRepository implementation."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.models import UserProfile, UserRole
from ..domain.repository import UserProfileRepository
from .orm_models import UserORM


class SQLAlchemyUserProfileRepository(UserProfileRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_telegram_id(self, telegram_id: int) -> UserProfile | None:
        result = await self._session.execute(
            select(UserORM).where(UserORM.telegram_id == telegram_id)
        )
        row = result.scalar_one_or_none()
        return self._to_domain(row) if row is not None else None

    async def get_by_chatwoot_id(self, chatwoot_user_id: int) -> UserProfile | None:
        result = await self._session.execute(
            select(UserORM).where(UserORM.chatwoot_user_id == chatwoot_user_id)
        )
        row = result.scalar_one_or_none()
        return self._to_domain(row) if row is not None else None

    async def save(self, profile: UserProfile) -> None:
        row = await self._session.get(UserORM, profile.telegram_id)
        if row is None:
            self._session.add(
                UserORM(
                    telegram_id=profile.telegram_id,
                    chatwoot_user_id=profile.chatwoot_user_id,
                    chatwoot_account_id=profile.chatwoot_account_id,
                    role=profile.role.value,
                    phone_internal=profile.phone_internal,
                    voice_sample_url=profile.voice_sample_url,
                    settings=profile.settings,
                    is_active=profile.is_active,
                    created_at=profile.created_at,
                )
            )
        else:
            row.chatwoot_user_id = profile.chatwoot_user_id
            row.chatwoot_account_id = profile.chatwoot_account_id
            row.role = profile.role.value
            row.phone_internal = profile.phone_internal
            row.voice_sample_url = profile.voice_sample_url
            row.settings = profile.settings
            row.is_active = profile.is_active

    async def list_active(self) -> list[UserProfile]:
        result = await self._session.execute(select(UserORM).where(UserORM.is_active.is_(True)))
        return [self._to_domain(row) for row in result.scalars().all()]

    @staticmethod
    def _to_domain(row: UserORM) -> UserProfile:
        settings: dict[str, Any] = row.settings if isinstance(row.settings, dict) else {}
        return UserProfile(
            telegram_id=row.telegram_id,
            chatwoot_user_id=row.chatwoot_user_id,
            chatwoot_account_id=row.chatwoot_account_id,
            role=UserRole(row.role),
            phone_internal=row.phone_internal,
            voice_sample_url=row.voice_sample_url,
            settings=settings,
            is_active=row.is_active,
            created_at=row.created_at,
        )
