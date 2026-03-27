"""Speech-to-Text — Abstract Repository and Port."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from .models import Transcription


class TranscriptionRepository(ABC):
    @abstractmethod
    async def get_by_id(self, transcription_id: uuid.UUID) -> Transcription | None: ...

    @abstractmethod
    async def get_by_source(self, source_file_id: str) -> Transcription | None: ...

    @abstractmethod
    async def save(self, transcription: Transcription) -> None: ...


class STTPort(ABC):
    """Anti-Corruption Layer: интерфейс к STT-сервису (Whisper)."""

    @abstractmethod
    async def transcribe(self, audio_file_path: str, language: str = "ru") -> str: ...
