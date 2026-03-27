"""Speech-to-Text — Application Use Cases."""
from __future__ import annotations

from typing import Any

from ..domain.models import Transcription
from ..domain.repository import STTPort, TranscriptionRepository

_CACHE_TTL = 86400  # 24 hours


class TranscribeAudio:
    """Транскрибирует аудио с кэшированием в Redis по file_id."""

    def __init__(
        self,
        stt_port: STTPort,
        repo: TranscriptionRepository,
        redis_client: Any,
    ) -> None:
        self._stt_port = stt_port
        self._repo = repo
        self._redis = redis_client

    async def execute(
        self, file_id: str, audio_file_path: str, language: str = "ru"
    ) -> Transcription:
        cache_key = f"stt:transcription:{file_id}"
        cached = await self._redis.get(cache_key)

        if cached:
            existing = await self._repo.get_by_source(file_id)
            if existing is not None:
                return existing
            # Восстанавливаем из кэша без повторной транскрипции
            transcription = Transcription(source_file_id=file_id, language=language)
            transcription.complete(cached.decode())
            return transcription

        transcription = Transcription(source_file_id=file_id, language=language)
        try:
            text = await self._stt_port.transcribe(audio_file_path, language)
            transcription.complete(text)
            await self._redis.setex(cache_key, _CACHE_TTL, text)
        except Exception as exc:
            transcription.fail(str(exc))

        await self._repo.save(transcription)
        return transcription
