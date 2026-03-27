"""Telegram Ingestion — SQLAlchemy PendingUserRepository implementation."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.models import PendingUser, UserRole
from ..domain.repository import PendingUserRepository
from .orm_models import PendingUserORM


class SQLAlchemyPendingUserRepository(PendingUserRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_phone(self, phone: str) -> PendingUser | None:
        result = await self._session.execute(
            select(PendingUserORM).where(PendingUserORM.phone == phone)
        )
        row = result.scalar_one_or_none()
        return self._to_domain(row) if row is not None else None

    async def save(self, pending: PendingUser) -> None:
        row = await self._session.get(PendingUserORM, pending.phone)
        if row is None:
            self._session.add(
                PendingUserORM(
                    phone=pending.phone,
                    chatwoot_user_id=pending.chatwoot_user_id,
                    chatwoot_account_id=pending.chatwoot_account_id,
                    role=pending.role.value,
                    created_at=pending.created_at,
                )
            )
        else:
            row.chatwoot_user_id = pending.chatwoot_user_id
            row.chatwoot_account_id = pending.chatwoot_account_id
            row.role = pending.role.value

    async def delete(self, phone: str) -> None:
        row = await self._session.get(PendingUserORM, phone)
        if row is not None:
            await self._session.delete(row)

    async def list_all(self) -> list[PendingUser]:
        result = await self._session.execute(select(PendingUserORM))
        return [self._to_domain(row) for row in result.scalars().all()]

    @staticmethod
    def _to_domain(row: PendingUserORM) -> PendingUser:
        return PendingUser(
            phone=row.phone,
            chatwoot_user_id=row.chatwoot_user_id,
            chatwoot_account_id=row.chatwoot_account_id,
            role=UserRole(row.role),
            created_at=row.created_at,
        )
