"""ATS Processing — SQLAlchemy ORM Models."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ATSBase(DeclarativeBase):
    pass


class CallRecordORM(ATSBase):
    __tablename__ = "ats_call_records"

    call_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    audio_url: Mapped[str] = mapped_column(String(500), nullable=False)
    transcription_t2: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcription_whisper: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    caller_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    agent_ext: Mapped[str | None] = mapped_column(String(10), nullable=True)
    detected_agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    voice_match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="new")
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
