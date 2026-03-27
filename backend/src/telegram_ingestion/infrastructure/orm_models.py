"""Telegram Ingestion — SQLAlchemy ORM Models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):  # type: ignore[misc]
    pass


class UserORM(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chatwoot_user_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    chatwoot_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="agent")
    phone_internal: Mapped[str | None] = mapped_column(String(20), nullable=True)
    voice_sample_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    settings: Mapped[Any] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class PendingUserORM(Base):
    __tablename__ = "pending_users"

    phone: Mapped[str] = mapped_column(String(20), primary_key=True)
    chatwoot_user_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    chatwoot_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="agent")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class DraftSessionORM(Base):
    __tablename__ = "draft_sessions"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(20), default="collecting")
    source_type: Mapped[str] = mapped_column(String(20), default="manual")
    call_record_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    content_blocks: Mapped[Any] = mapped_column(JSON, default=list)
    assembled_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_result: Mapped[Any] = mapped_column(JSON, nullable=True)
    preview_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
