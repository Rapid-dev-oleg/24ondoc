"""Task Events — SQLAlchemy ORM Models."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class TaskEventsBase(DeclarativeBase):
    pass


class TaskEventORM(TaskEventsBase):
    __tablename__ = "task_events"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    twenty_task_id: Mapped[str] = mapped_column(String, nullable=False)
    # DB-level FK to users(telegram_id) ON DELETE SET NULL is kept in
    # alembic migration 0008; declaring it in the ORM here would force
    # SQLAlchemy to resolve a table defined under a different Base and
    # fail at flush with NoReferencedTableError.
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    location_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[str | None] = mapped_column(String, nullable=True)
    problem_signature: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    script_violations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    script_missing: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    meta: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
