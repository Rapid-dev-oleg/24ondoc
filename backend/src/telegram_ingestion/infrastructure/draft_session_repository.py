"""Telegram Ingestion — SQLAlchemy + Redis DraftSessionRepository implementation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.models import (
    AIResult,
    ContentBlock,
    DraftSession,
    SessionStatus,
    SourceType,
)
from ..domain.repository import DraftSessionRepository
from .orm_models import DraftSessionORM

_REDIS_KEY_PREFIX = "draft:active:"
_TTL_SECONDS = 86400  # 24 hours


class SQLAlchemyRedisDraftSessionRepository(DraftSessionRepository):
    def __init__(self, session: AsyncSession, redis: AsyncRedis) -> None:
        self._session = session
        self._redis: AsyncRedis = redis

    async def get_by_id(self, session_id: uuid.UUID) -> DraftSession | None:
        row = await self._session.get(DraftSessionORM, session_id)
        return self._to_domain(row) if row is not None else None

    async def get_active_by_user(self, user_id: int) -> DraftSession | None:
        redis_key = f"{_REDIS_KEY_PREFIX}{user_id}"
        raw = await self._redis.get(redis_key)
        if raw is not None:
            session_id = uuid.UUID(raw.decode())
            return await self.get_by_id(session_id)
        # Redis miss — fallback to PostgreSQL
        now = datetime.now(UTC)
        result = await self._session.execute(
            select(DraftSessionORM)
            .where(
                DraftSessionORM.user_id == user_id,
                DraftSessionORM.status.in_(["collecting", "analyzing", "preview", "editing"]),
                (DraftSessionORM.expires_at > now) | (DraftSessionORM.expires_at.is_(None)),
            )
            .order_by(DraftSessionORM.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return self._to_domain(row) if row is not None else None

    async def save(self, session: DraftSession) -> None:
        row = await self._session.get(DraftSessionORM, session.session_id)
        if row is None:
            self._session.add(self._to_orm(session))
        else:
            row.status = session.status.value
            row.content_blocks = [b.model_dump(mode="json") for b in session.content_blocks]
            row.assembled_text = session.assembled_text
            row.ai_result = session.ai_result.model_dump(mode="json") if session.ai_result else None
            row.preview_message_id = session.preview_message_id
            row.updated_at = session.updated_at
        redis_key = f"{_REDIS_KEY_PREFIX}{session.user_id}"
        await self._redis.set(redis_key, str(session.session_id), ex=_TTL_SECONDS)

    async def delete(self, session_id: uuid.UUID) -> None:
        row = await self._session.get(DraftSessionORM, session_id)
        if row is not None:
            redis_key = f"{_REDIS_KEY_PREFIX}{row.user_id}"
            await self._redis.delete(redis_key)
            await self._session.delete(row)

    @staticmethod
    def _to_domain(row: DraftSessionORM) -> DraftSession:
        blocks_raw: list[dict[str, Any]] = cast(list[dict[str, Any]], row.content_blocks or [])
        ai_raw: dict[str, Any] | None = cast("dict[str, Any] | None", row.ai_result)
        return DraftSession(
            session_id=row.session_id,
            user_id=row.user_id,
            status=SessionStatus(row.status),
            source_type=SourceType(row.source_type),
            call_record_id=row.call_record_id,
            content_blocks=[ContentBlock(**b) for b in blocks_raw],
            assembled_text=row.assembled_text,
            ai_result=AIResult(**ai_raw) if ai_raw else None,
            preview_message_id=row.preview_message_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            expires_at=row.expires_at,
        )

    @staticmethod
    def _to_orm(session: DraftSession) -> DraftSessionORM:
        return DraftSessionORM(
            session_id=session.session_id,
            user_id=session.user_id,
            status=session.status.value,
            source_type=session.source_type.value,
            call_record_id=session.call_record_id,
            content_blocks=[b.model_dump(mode="json") for b in session.content_blocks],
            assembled_text=session.assembled_text,
            ai_result=(session.ai_result.model_dump(mode="json") if session.ai_result else None),
            preview_message_id=session.preview_message_id,
            created_at=session.created_at,
            updated_at=session.updated_at,
            expires_at=session.expires_at,
        )
