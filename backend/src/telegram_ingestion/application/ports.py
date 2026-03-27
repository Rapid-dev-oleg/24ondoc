"""Telegram Ingestion — Application Ports (Interfaces)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.models import UserProfile


class STTPort(ABC):
    """Port for Speech-to-Text transcription."""

    @abstractmethod
    async def transcribe(self, file_bytes: bytes) -> str:
        """Transcribe audio bytes to text string."""
        ...


class UserProfilePort(ABC):
    """Port for user profile queries."""

    @abstractmethod
    async def is_authorized(self, telegram_id: int) -> bool:
        """Return True if telegram user is authorized in the system."""
        ...

    @abstractmethod
    async def get_profile(self, telegram_id: int) -> UserProfile | None:
        """Return UserProfile by telegram_id or None if not found."""
        ...

    @abstractmethod
    async def list_active_agents(self) -> list[UserProfile]:
        """Return list of all active agent profiles."""
        ...
