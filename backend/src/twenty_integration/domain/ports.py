"""Twenty Integration — Abstract Port."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from .models import TwentyMember, TwentyPerson, TwentyTask


class TwentyCRMPort(ABC):
    """Anti-Corruption Layer: интерфейс к Twenty CRM REST API."""

    @abstractmethod
    async def list_workspace_members(self) -> list[TwentyMember]: ...

    @abstractmethod
    async def find_person_by_telegram_id(self, telegram_id: int) -> TwentyPerson | None: ...

    @abstractmethod
    async def create_person(self, telegram_id: int, name: str) -> TwentyPerson: ...

    @abstractmethod
    async def create_task(
        self, title: str, body: str, due_at: datetime | None, assignee_id: str | None
    ) -> TwentyTask: ...

    @abstractmethod
    async def link_person_to_task(self, task_id: str, person_id: str) -> None: ...
