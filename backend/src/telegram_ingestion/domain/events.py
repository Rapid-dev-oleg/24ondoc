"""Telegram Ingestion — Domain Events."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class DomainEvent:
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class MessageReceived(DomainEvent):
    session_id: uuid.UUID = field(default_factory=uuid.uuid4)
    user_id: int = 0
    content_type: str = "text"


@dataclass(frozen=True)
class VoiceReceived(DomainEvent):
    session_id: uuid.UUID = field(default_factory=uuid.uuid4)
    user_id: int = 0
    file_id: str = ""


@dataclass(frozen=True)
class SessionReadyForAnalysis(DomainEvent):
    session_id: uuid.UUID = field(default_factory=uuid.uuid4)
    user_id: int = 0
    assembled_text: str = ""


@dataclass(frozen=True)
class SessionAnalysisCompleted(DomainEvent):
    session_id: uuid.UUID = field(default_factory=uuid.uuid4)
    user_id: int = 0


@dataclass(frozen=True)
class TaskCreatedInCRM(DomainEvent):
    session_id: uuid.UUID = field(default_factory=uuid.uuid4)
    user_id: int = 0
    chatwoot_conversation_id: int = 0
