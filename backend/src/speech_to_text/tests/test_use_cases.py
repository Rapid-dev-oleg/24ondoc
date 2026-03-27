"""Tests for Speech-to-Text use cases."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ..application.use_cases import TranscribeAudio
from ..domain.models import Transcription, TranscriptionStatus
from ..domain.repository import STTPort, TranscriptionRepository


@pytest.fixture()
def mock_stt() -> STTPort:
    stt = AsyncMock(spec=STTPort)
    stt.transcribe.return_value = "Привет мир"
    return stt


@pytest.fixture()
def mock_repo() -> TranscriptionRepository:
    repo = AsyncMock(spec=TranscriptionRepository)
    repo.get_by_source.return_value = None
    return repo


@pytest.fixture()
def mock_redis() -> MagicMock:
    r = AsyncMock()
    r.get.return_value = None
    return r


@pytest.mark.asyncio
async def test_transcribe_audio_calls_stt_and_saves(
    mock_stt: STTPort,
    mock_repo: TranscriptionRepository,
    mock_redis: MagicMock,
) -> None:
    use_case = TranscribeAudio(stt_port=mock_stt, repo=mock_repo, redis_client=mock_redis)
    result = await use_case.execute("file_123", "/tmp/audio.ogg")

    assert result.status == TranscriptionStatus.COMPLETED
    assert result.text == "Привет мир"
    mock_stt.transcribe.assert_awaited_once_with("/tmp/audio.ogg", "ru")  # type: ignore[attr-defined]
    mock_repo.save.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_transcribe_audio_returns_cached_when_available(
    mock_stt: STTPort,
    mock_repo: TranscriptionRepository,
    mock_redis: MagicMock,
) -> None:
    mock_redis.get.return_value = b"cached text"
    cached_transcription = Transcription(source_file_id="file_123", language="ru")
    cached_transcription.complete("cached text")
    mock_repo.get_by_source.return_value = cached_transcription  # type: ignore[attr-defined]

    use_case = TranscribeAudio(stt_port=mock_stt, repo=mock_repo, redis_client=mock_redis)
    result = await use_case.execute("file_123", "/tmp/audio.ogg")

    assert result.text == "cached text"
    mock_stt.transcribe.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_transcribe_audio_rebuilds_from_cache_if_no_db_record(
    mock_stt: STTPort,
    mock_repo: TranscriptionRepository,
    mock_redis: MagicMock,
) -> None:
    mock_redis.get.return_value = b"cached text"
    mock_repo.get_by_source.return_value = None  # type: ignore[attr-defined]

    use_case = TranscribeAudio(stt_port=mock_stt, repo=mock_repo, redis_client=mock_redis)
    result = await use_case.execute("file_123", "/tmp/audio.ogg")

    assert result.text == "cached text"
    assert result.status == TranscriptionStatus.COMPLETED
    mock_stt.transcribe.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_transcribe_audio_caches_result(
    mock_stt: STTPort,
    mock_repo: TranscriptionRepository,
    mock_redis: MagicMock,
) -> None:
    use_case = TranscribeAudio(stt_port=mock_stt, repo=mock_repo, redis_client=mock_redis)
    await use_case.execute("file_456", "/tmp/audio.ogg")

    mock_redis.setex.assert_awaited_once_with("stt:transcription:file_456", 86400, "Привет мир")


@pytest.mark.asyncio
async def test_transcribe_audio_fails_gracefully(
    mock_stt: STTPort,
    mock_repo: TranscriptionRepository,
    mock_redis: MagicMock,
) -> None:
    mock_stt.transcribe.side_effect = ConnectionError("STT unavailable")  # type: ignore[attr-defined]
    use_case = TranscribeAudio(stt_port=mock_stt, repo=mock_repo, redis_client=mock_redis)
    result = await use_case.execute("file_789", "/tmp/audio.ogg")

    assert result.status == TranscriptionStatus.FAILED
    assert result.error_message is not None
    assert "STT unavailable" in result.error_message
    mock_repo.save.assert_awaited_once()  # type: ignore[attr-defined]
