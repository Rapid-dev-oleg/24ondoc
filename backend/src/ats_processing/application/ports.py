"""ATS Processing — Application Ports (interfaces)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime


class AudioStoragePort(ABC):
    """Port for storing audio files (MinIO / S3)."""

    @abstractmethod
    async def upload(self, key: str, data: bytes, content_type: str = "audio/ogg") -> str:
        """Upload audio bytes under key, return the storage path."""
        ...

    @abstractmethod
    async def get_presigned_url(self, key: str) -> str:
        """Return a presigned URL for the stored file."""
        ...


class VoiceEmbeddingPort(ABC):
    """Port for extracting voice embeddings via Whisper encoder."""

    @abstractmethod
    async def embed(self, audio_bytes: bytes) -> list[float]:
        """Return a float vector (384-dim) from audio bytes."""
        ...


class ATS2CallSourcePort(ABC):
    """Port for fetching call data from ATS2 REST API."""

    @abstractmethod
    async def get_call_records(
        self,
        date_from: datetime,
        date_to: datetime,
    ) -> list[dict[str, object]]:
        """Return call records filtered by date range."""
        ...

    @abstractmethod
    async def download_recording(self, filename: str) -> bytes:
        """Download MP3 recording by filename."""
        ...

    @abstractmethod
    async def get_transcription(self, filename: str) -> dict[str, object]:
        """Get STT transcription for a recording."""
        ...

    @abstractmethod
    async def get_active_calls(self) -> list[dict[str, object]]:
        """Get currently active calls from monitoring."""
        ...

    @abstractmethod
    async def get_employees(self) -> list[dict[str, object]]:
        """Get list of ATS2 employees."""
        ...
