"""Speech-to-Text — Domain Models."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class TranscriptionStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class Transcription(BaseModel):
    """Aggregate Root: транскрипция аудио."""

    transcription_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    source_file_id: str
    language: str = "ru"
    text: str | None = None
    status: TranscriptionStatus = TranscriptionStatus.PENDING
    error_message: str | None = None
    duration_seconds: float | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def complete(self, text: str, duration_seconds: float | None = None) -> None:
        if self.status != TranscriptionStatus.PENDING:
            raise ValueError(f"Cannot complete transcription in status: {self.status}")
        self.text = text
        self.duration_seconds = duration_seconds
        self.status = TranscriptionStatus.COMPLETED

    def fail(self, reason: str) -> None:
        self.error_message = reason
        self.status = TranscriptionStatus.FAILED
