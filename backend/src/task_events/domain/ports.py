"""Task Events — Ports."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from .models import Action, TaskEvent


class TaskEventRepository(ABC):
    """Persistence port for task events (local append-only log)."""

    @abstractmethod
    async def add(self, event: TaskEvent) -> None: ...

    @abstractmethod
    async def find_recent_by_location(
        self,
        location_phone: str,
        *,
        since: datetime,
        limit: int = 10,
        action: Action = Action.CREATED,
    ) -> list[TaskEvent]: ...

    @abstractmethod
    async def has_action_for_task(
        self, twenty_task_id: str, action: Action
    ) -> bool: ...

    @abstractmethod
    async def recent_by_task(
        self, twenty_task_id: str, limit: int = 20
    ) -> list[TaskEvent]: ...
