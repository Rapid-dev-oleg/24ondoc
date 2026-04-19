"""Task Events — Domain Models."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Action(StrEnum):
    CREATED = "CREATED"
    ASSIGNED = "ASSIGNED"
    STATUS_CHANGED = "STATUS_CHANGED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    COMMENT_ADDED = "COMMENT_ADDED"
    SCRIPT_CHECKED = "SCRIPT_CHECKED"
    REPEAT_CHECKED = "REPEAT_CHECKED"


class ActorType(StrEnum):
    OPERATOR = "OPERATOR"
    ADMIN = "ADMIN"
    SYSTEM_AI = "SYSTEM_AI"


class Source(StrEnum):
    CALL = "call"
    MANUAL = "manual"
    WEBHOOK = "webhook"


class TaskEvent(BaseModel):
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    twenty_task_id: str
    user_id: int | None = None
    location_phone: str | None = None
    action: Action
    priority: str | None = None
    problem_signature: str | None = None
    parent_task_id: str | None = None
    script_violations: int | None = None
    script_missing: list[str] | None = None
    source: Source | None = None
    meta: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
