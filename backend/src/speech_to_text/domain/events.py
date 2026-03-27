"""Speech-to-Text — Domain Events."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class DomainEvent:
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class TranscriptionCompleted(DomainEvent):
    transcription_id: uuid.UUID = field(default_factory=uuid.uuid4)
    source_file_id: str = ""
    text: str = ""


@dataclass(frozen=True)
class TranscriptionFailed(DomainEvent):
    transcription_id: uuid.UUID = field(default_factory=uuid.uuid4)
    source_file_id: str = ""
    reason: str = ""
