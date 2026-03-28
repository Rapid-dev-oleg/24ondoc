"""Chatwoot Integration — Domain Models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class TicketStatus(StrEnum):
    OPEN = "open"
    PENDING = "pending"
    RESOLVED = "resolved"
    SNOOZED = "snoozed"


class CreateTicketCommand(BaseModel):
    """Value Object: команда создания задачи в Chatwoot."""

    title: str
    description: str
    priority: str
    category: str
    assignee_chatwoot_id: int | None = None
    labels: list[str] = Field(default_factory=list)
    deadline: str | None = None
    source_session_id: uuid.UUID | None = None
    contact_id: int | None = None


class SupportTicket(BaseModel):
    """Aggregate Root: задача в Chatwoot (зеркало)."""

    task_id: int
    source_session_id: uuid.UUID | None = None
    assignee_telegram_id: int | None = None
    assignee_chatwoot_id: int | None = None
    status: TicketStatus = TicketStatus.OPEN
    priority: str = "medium"
    title: str = ""
    permalink: str = ""
    labels: list[str] = Field(default_factory=list)
    last_sync: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def update_status(self, new_status: TicketStatus) -> None:
        self.status = new_status
        self.last_sync = datetime.now(UTC)

    def reassign(self, telegram_id: int) -> None:
        self.assignee_telegram_id = telegram_id
        self.last_sync = datetime.now(UTC)

    def update_fields(
        self,
        priority: str | None = None,
        labels: list[str] | None = None,
        assignee_chatwoot_id: int | None = None,
    ) -> None:
        """Синхронизирует поля из conversation_updated вебхука."""
        if priority is not None:
            self.priority = priority
        if labels is not None:
            self.labels = labels
        if assignee_chatwoot_id is not None:
            self.assignee_chatwoot_id = assignee_chatwoot_id
        self.last_sync = datetime.now(UTC)


class ChatwootAgent(BaseModel):
    """Value Object: агент Chatwoot, созданный через Platform API."""

    user_id: int
    access_token: str = ""
    sso_url: str = ""
