"""ATS Processing — Abstract Repository."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from .models import CallRecord, SourceType


class CallRecordRepository(ABC):
    @abstractmethod
    async def get_by_id(self, call_id: str) -> CallRecord | None: ...

    @abstractmethod
    async def save(self, record: CallRecord) -> None: ...

    @abstractmethod
    async def get_pending(
        self, limit: int = 10, source: SourceType | None = None
    ) -> list[CallRecord]: ...

    @abstractmethod
    async def find_recent_by_phone(self, phone: str, limit: int = 10) -> list[CallRecord]: ...

    @abstractmethod
    async def get_recent(self, limit: int = 10) -> list[CallRecord]: ...

    @abstractmethod
    async def set_twenty_task_by_session(
        self, session_id: uuid.UUID, twenty_task_id: str
    ) -> bool:
        """Attach a freshly-created Twenty Task to the call that produced the draft.

        Returns True if a matching ats_call_records row was updated, False if
        no call is linked to this draft (i.e. task was created outside the
        call flow — manual Telegram /new_task).
        """
        ...


class AgentVoiceSampleRepository(ABC):
    """Repository for agent voice embeddings (pgvector)."""

    @abstractmethod
    async def find_closest(self, embedding: list[float]) -> tuple[int, float] | None:
        """Return (agent_id, score) of the closest embedding, or None if empty."""
        ...

    @abstractmethod
    async def save(self, agent_id: int, embedding: list[float]) -> None:
        """Save or overwrite the voice embedding for the given agent_id."""
        ...
