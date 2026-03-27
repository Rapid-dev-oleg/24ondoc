"""AI Classification — Domain Events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class DomainEvent:
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class RequestClassified(DomainEvent):
    result_id: uuid.UUID = field(default_factory=uuid.uuid4)
    session_id: uuid.UUID = field(default_factory=uuid.uuid4)
    priority: str = "medium"
    category: str = "other"


@dataclass(frozen=True)
class ClassificationFailed(DomainEvent):
    session_id: uuid.UUID = field(default_factory=uuid.uuid4)
    reason: str = ""
