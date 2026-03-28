"""ATS Processing — Abstract Repository."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import CallRecord


class CallRecordRepository(ABC):
    @abstractmethod
    async def get_by_id(self, call_id: str) -> CallRecord | None: ...

    @abstractmethod
    async def save(self, record: CallRecord) -> None: ...

    @abstractmethod
    async def get_pending(self, limit: int = 10) -> list[CallRecord]: ...

    @abstractmethod
    async def find_recent_by_phone(self, phone: str, limit: int = 10) -> list[CallRecord]: ...


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
