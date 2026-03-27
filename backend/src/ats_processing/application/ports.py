"""ATS Processing — Application Ports (interfaces)."""
from __future__ import annotations

from abc import ABC, abstractmethod


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
