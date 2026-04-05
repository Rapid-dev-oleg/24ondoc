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

    @abstractmethod
    async def update_twenty_member_id(
        self, telegram_id: int, twenty_member_id: str
    ) -> UserProfile | None:
        """Update twenty_member_id for a user. Returns updated profile or None if not found."""
        ...

    @abstractmethod
    async def upsert_user(
        self,
        telegram_id: int,
        twenty_member_id: str,
        role: str,
        display_name: str = "",
    ) -> UserProfile:
        """Create or update a user with role and twenty_member_id."""
        ...


class VoiceSampleStoragePort(ABC):
    """Port for persisting voice sample audio files."""

    @abstractmethod
    async def save(self, telegram_id: int, data: bytes, ext: str) -> str:
        """Save audio bytes and return a path/URL string for storage in the user profile."""
        ...


class VoiceEnrollmentPort(ABC):
    """ACL port for enrolling a voice sample into the biometric recognition system."""

    @abstractmethod
    async def enroll(self, agent_id: int, audio_bytes: bytes) -> bool:
        """Enroll audio_bytes for the given agent_id. Returns True on success."""
        ...
