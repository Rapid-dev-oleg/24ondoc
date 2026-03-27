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
