"""Twenty Integration — Domain Models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class TwentyMember(BaseModel):
    """Value Object: участник рабочего пространства Twenty."""

    twenty_id: str  # UUID workspace member
    first_name: str
    last_name: str
    email: str


class TwentyPerson(BaseModel):
    """Value Object: контакт/персона в Twenty."""

    twenty_id: str
    telegram_id: int
    name: str


class TwentyTask(BaseModel):
    """Aggregate Root: задача в Twenty CRM."""

    twenty_id: str
    title: str
    body: str
    status: str  # "TODO" | "IN_PROGRESS" | "DONE"
    due_at: datetime | None = None
    assignee_id: str | None = None
    person_id: str | None = None
