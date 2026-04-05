"""AI Classification — Domain Models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Category(StrEnum):
    BUG = "bug"
    FEATURE = "feature"
    QUESTION = "question"
    COMPLAINT = "complaint"
    OTHER = "other"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class ClassificationEntities(BaseModel):
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    prices: list[float] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)


class ClassificationResult(BaseModel):
    """Aggregate Root: результат AI-классификации обращения."""

    result_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    source_text: str
    title: str
    description: str
    category: Category
    priority: Priority
    deadline: str | None = None
    entities: ClassificationEntities = Field(default_factory=ClassificationEntities)
    assignee_hint: str | None = None
    model_used: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def is_urgent(self) -> bool:
        return self.priority in (Priority.URGENT, Priority.HIGH)


class TaskFieldSelection(BaseModel):
    """Result of AI selecting kategoriya and vazhnost from Twenty option lists."""

    kategoriya: str | None = None
    vazhnost: str | None = None
