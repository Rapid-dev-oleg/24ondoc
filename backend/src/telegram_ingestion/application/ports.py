"""Telegram Ingestion — Application Ports (Interfaces)."""

from __future__ import annotations

from abc import ABC, abstractmethod


class STTPort(ABC):
    """Port for Speech-to-Text transcription."""

    @abstractmethod
    async def transcribe(self, file_bytes: bytes) -> str:
        """Transcribe audio bytes to text string."""
        ...


class UserProfilePort(ABC):
    """Port for user authorization check."""

    @abstractmethod
    async def is_authorized(self, telegram_id: int) -> bool:
        """Return True if telegram user is authorized in the system."""
        ...
