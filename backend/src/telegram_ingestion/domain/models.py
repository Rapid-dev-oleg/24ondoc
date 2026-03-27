"""Telegram Ingestion — Domain Models (Aggregates, Value Objects)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class UserRole(str, Enum):
    AGENT = "agent"
    SUPERVISOR = "supervisor"
    ADMIN = "admin"


class UserProfile(BaseModel):
    """Aggregate Root: профиль пользователя Telegram/Chatwoot."""

    telegram_id: int
    chatwoot_user_id: int
    chatwoot_account_id: int
    role: UserRole = UserRole.AGENT
    phone_internal: str | None = None
    voice_sample_url: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SessionStatus(str, Enum):
    COLLECTING = "collecting"
    ANALYZING = "analyzing"
    PREVIEW = "preview"
    EDITING = "editing"


class SourceType(str, Enum):
    MANUAL = "manual"
    CALL_T2 = "call_t2"


class ContentBlock(BaseModel):
    """Value Object: единица контента в сессии."""

    type: str  # text | voice | file | photo
    content: str
    file_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AIResult(BaseModel):
    """Value Object: результат анализа от OpenRouter."""

    title: str
    description: str
    category: str  # bug | feature | question | complaint | other
    priority: str  # low | medium | high | urgent
    deadline: str | None = None
    entities: dict[str, list[Any]] = Field(default_factory=dict)
    assignee_hint: str | None = None


class DraftSession(BaseModel):
    """Aggregate Root: сессия сбора задачи."""

    session_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    user_id: int
    status: SessionStatus = SessionStatus.COLLECTING
    source_type: SourceType = SourceType.MANUAL
    call_record_id: uuid.UUID | None = None
    content_blocks: list[ContentBlock] = Field(default_factory=list)
    assembled_text: str | None = None
    ai_result: AIResult | None = None
    preview_message_id: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None

    def add_content_block(self, block: ContentBlock) -> None:
        if self.status not in (SessionStatus.COLLECTING, SessionStatus.EDITING):
            raise ValueError(f"Cannot add content in status: {self.status}")
        self.content_blocks.append(block)
        self._touch()

    def assemble_text(self) -> str:
        parts = [b.content for b in self.content_blocks if b.content]
        self.assembled_text = "\n".join(parts)
        return self.assembled_text

    def start_analysis(self) -> None:
        if self.status not in (SessionStatus.COLLECTING, SessionStatus.EDITING):
            raise ValueError(f"Cannot start analysis in status: {self.status}")
        self.assembled_text = self.assemble_text()
        self.status = SessionStatus.ANALYZING
        self._touch()

    def complete_analysis(self, result: AIResult) -> None:
        if self.status != SessionStatus.ANALYZING:
            raise ValueError(f"Cannot complete analysis in status: {self.status}")
        self.ai_result = result
        self.status = SessionStatus.PREVIEW
        self._touch()

    def start_editing(self) -> None:
        if self.status != SessionStatus.PREVIEW:
            raise ValueError(f"Cannot edit in status: {self.status}")
        self.status = SessionStatus.EDITING
        self._touch()

    def _touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)
