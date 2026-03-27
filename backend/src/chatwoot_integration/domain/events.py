"""Chatwoot Integration — Domain Events."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class DomainEvent:
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class TicketCreated(DomainEvent):
    task_id: int = 0
    session_id: uuid.UUID = field(default_factory=uuid.uuid4)
    permalink: str = ""


@dataclass(frozen=True)
class TicketUpdated(DomainEvent):
    task_id: int = 0
    new_status: str = ""


@dataclass(frozen=True)
class TicketCreationFailed(DomainEvent):
    session_id: uuid.UUID = field(default_factory=uuid.uuid4)
    reason: str = ""
