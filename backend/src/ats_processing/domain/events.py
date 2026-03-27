"""ATS Processing — Domain Events."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class DomainEvent:
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class CallWebhookReceived(DomainEvent):
    call_id: str = ""
    audio_url: str = ""
    agent_ext: str = ""


@dataclass(frozen=True)
class AudioDownloaded(DomainEvent):
    call_id: str = ""
    local_path: str = ""


@dataclass(frozen=True)
class CallAgentIdentified(DomainEvent):
    call_id: str = ""
    agent_id: int = 0
    score: float = 0.0


@dataclass(frozen=True)
class CallProcessingFailed(DomainEvent):
    call_id: str = ""
    reason: str = ""
