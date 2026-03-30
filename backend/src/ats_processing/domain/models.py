"""ATS Processing — Domain Models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class CallStatus(StrEnum):
    NEW = "new"
    PROCESSING = "processing"
    PREVIEW = "preview"
    CREATED = "created"
    ERROR = "error"


class SourceType(StrEnum):
    CALL_T2_WEBHOOK = "call_t2_webhook"
    CALL_ATS2_POLLING = "call_ats2_polling"


class CallRecord(BaseModel):
    """Aggregate Root: запись звонка от АТС Т2."""

    call_id: str
    audio_url: str
    source: SourceType = SourceType.CALL_T2_WEBHOOK
    transcription_t2: str | None = None
    transcription_whisper: str | None = None
    duration: int | None = None
    caller_phone: str | None = None
    agent_ext: str | None = None
    detected_agent_id: int | None = None
    voice_match_score: float | None = None
    status: CallStatus = CallStatus.NEW
    session_id: uuid.UUID | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def start_processing(self) -> None:
        if self.status != CallStatus.NEW:
            raise ValueError(f"Cannot process call in status: {self.status}")
        self.status = CallStatus.PROCESSING

    def set_transcription(self, text: str, source: str = "whisper") -> None:
        if source == "t2":
            self.transcription_t2 = text
        else:
            self.transcription_whisper = text

    def get_best_transcription(self) -> str | None:
        return self.transcription_whisper or self.transcription_t2

    def set_voice_match(self, agent_id: int, score: float) -> None:
        if not (0.0 <= score <= 1.0):
            raise ValueError("Voice match score must be between 0 and 1")
        self.detected_agent_id = agent_id
        self.voice_match_score = score

    def mark_preview(self, session_id: uuid.UUID) -> None:
        self.session_id = session_id
        self.status = CallStatus.PREVIEW

    def mark_created(self) -> None:
        if self.status != CallStatus.PREVIEW:
            raise ValueError("Can only mark created from preview state")
        self.status = CallStatus.CREATED

    def mark_error(self) -> None:
        self.status = CallStatus.ERROR
