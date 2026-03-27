"""Tests for ATS Processing use cases (DEV-51, DEV-52)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ..application.ports import AudioStoragePort, VoiceEmbeddingPort
from ..application.use_cases import FetchAudioRecording, IdentifyAgentByVoice
from ..domain.models import CallRecord, CallStatus
from ..domain.repository import AgentVoiceSampleRepository, CallRecordRepository


# ---------- Stubs ----------


class StubAudioStorage(AudioStoragePort):
    def __init__(self, path: str = "voice-samples/calls/t2_001.ogg") -> None:
        self._path = path
        self.uploaded: list[tuple[str, bytes]] = []

    async def upload(self, key: str, data: bytes, content_type: str = "audio/ogg") -> str:
        self.uploaded.append((key, data))
        return self._path

    async def get_presigned_url(self, key: str) -> str:
        return f"http://minio:9000/{key}?presigned"


class StubCallRepo(CallRecordRepository):
    def __init__(self) -> None:
        self.saved: list[CallRecord] = []

    async def get_by_id(self, call_id: str) -> CallRecord | None:
        return next((r for r in self.saved if r.call_id == call_id), None)

    async def save(self, record: CallRecord) -> None:
        self.saved.append(record)

    async def get_pending(self, limit: int = 10) -> list[CallRecord]:
        return []

    async def find_recent_by_phone(self, phone: str, limit: int = 10) -> list[CallRecord]:
        return []


class StubEmbeddingPort(VoiceEmbeddingPort):
    def __init__(self, embedding: list[float] | None = None) -> None:
        self._embedding = embedding or [0.1] * 384
        self.call_count = 0

    async def embed(self, audio_bytes: bytes) -> list[float]:
        self.call_count += 1
        return self._embedding


class StubVoiceRepo(AgentVoiceSampleRepository):
    def __init__(self, closest: tuple[int, float] | None = None) -> None:
        self._closest = closest

    async def find_closest(self, embedding: list[float]) -> tuple[int, float] | None:
        return self._closest


def _make_call(call_id: str = "t2_001") -> CallRecord:
    return CallRecord(
        call_id=call_id,
        audio_url="https://t2.example.com/rec/001.mp3",
    )


# ============================================================
# DEV-51: FetchAudioRecording
# ============================================================


class TestFetchAudioRecording:
    async def test_fetch_audio_downloads_and_stores_in_minio(self) -> None:
        """AC: аудио скачивается и сохраняется в MinIO."""
        storage = StubAudioStorage()
        repo = StubCallRepo()
        use_case = FetchAudioRecording(audio_storage=storage, call_repo=repo)

        audio_data = b"fake-ogg-data"
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_response = MagicMock()
            mock_response.content = audio_data
            mock_response.raise_for_status = MagicMock()
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            call = _make_call()
            result = await use_case.execute(call)

        assert result == "voice-samples/calls/t2_001.ogg"
        assert len(storage.uploaded) == 1
        assert storage.uploaded[0][1] == audio_data

    async def test_fetch_audio_updates_call_record_status(self) -> None:
        """AC: статус CallRecord обновляется до PROCESSING."""
        storage = StubAudioStorage()
        repo = StubCallRepo()
        use_case = FetchAudioRecording(audio_storage=storage, call_repo=repo)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_response = MagicMock()
            mock_response.content = b"audio"
            mock_response.raise_for_status = MagicMock()
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            call = _make_call()
            await use_case.execute(call)

        assert call.status == CallStatus.PROCESSING

    async def test_fetch_audio_triggers_whisper_transcription(self) -> None:
        """AC: транскрипция запускается при наличии stt_port."""
        storage = StubAudioStorage()
        repo = StubCallRepo()

        stt_port = AsyncMock()
        stt_port.transcribe = AsyncMock(return_value="Привет из Whisper")
        use_case = FetchAudioRecording(audio_storage=storage, call_repo=repo, stt_port=stt_port)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_response = MagicMock()
            mock_response.content = b"audio"
            mock_response.raise_for_status = MagicMock()
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            call = _make_call()
            await use_case.execute(call)

        stt_port.transcribe.assert_awaited_once()
        assert call.transcription_whisper == "Привет из Whisper"

    async def test_fetch_audio_retries_on_timeout(self) -> None:
        """AC: retry при таймауте (tenacity)."""
        storage = StubAudioStorage()
        repo = StubCallRepo()
        use_case = FetchAudioRecording(audio_storage=storage, call_repo=repo)

        call_count = 0

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            async def fail_then_succeed(url: str) -> MagicMock:
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise httpx.TimeoutException("timeout")
                resp = MagicMock()
                resp.content = b"audio"
                resp.raise_for_status = MagicMock()
                return resp

            mock_client.get = fail_then_succeed
            mock_client_cls.return_value = mock_client

            call = _make_call()
            await use_case.execute(call)

        assert call_count == 3
        assert call.status == CallStatus.PROCESSING

    async def test_fetch_audio_raises_after_all_retries_exhausted(self) -> None:
        """Если все retry исчерпаны — пробрасывает исключение."""
        storage = StubAudioStorage()
        repo = StubCallRepo()
        use_case = FetchAudioRecording(audio_storage=storage, call_repo=repo)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_client_cls.return_value = mock_client

            call = _make_call()
            with pytest.raises(httpx.TimeoutException):
                await use_case.execute(call)

        assert len(repo.saved) == 0


# ============================================================
# DEV-52: IdentifyAgentByVoice
# ============================================================


class TestIdentifyAgentByVoice:
    async def test_voice_match_above_threshold_sets_detected_agent_id(self) -> None:
        """AC: score >= 0.85 → detected_agent_id заполнен."""
        embedding_port = StubEmbeddingPort()
        voice_repo = StubVoiceRepo(closest=(42, 0.92))
        use_case = IdentifyAgentByVoice(embedding_port, voice_repo, threshold=0.85)

        call = _make_call()
        result = await use_case.execute(call, audio_bytes=b"audio")

        assert result == 42
        assert call.detected_agent_id == 42
        assert call.voice_match_score == pytest.approx(0.92)

    async def test_voice_match_below_threshold_uses_regex_fallback(self) -> None:
        """AC: score < 0.85 → fallback на regex (agent_id не установлен)."""
        embedding_port = StubEmbeddingPort()
        voice_repo = StubVoiceRepo(closest=(42, 0.70))
        use_case = IdentifyAgentByVoice(embedding_port, voice_repo, threshold=0.85)

        call = _make_call()
        call.set_transcription("Это агент Иван Петров на линии.", source="whisper")
        result = await use_case.execute(call, audio_bytes=b"audio")

        # regex matches but no agent_id lookup
        assert result is None
        assert call.detected_agent_id is None

    async def test_voice_match_no_samples_returns_none(self) -> None:
        """AC: нет записей в БД → None."""
        embedding_port = StubEmbeddingPort()
        voice_repo = StubVoiceRepo(closest=None)
        use_case = IdentifyAgentByVoice(embedding_port, voice_repo)

        call = _make_call()
        result = await use_case.execute(call, audio_bytes=b"audio")

        assert result is None
        assert call.detected_agent_id is None

    async def test_embedding_extraction_calls_whisper_encoder(self) -> None:
        """AC: вызов Whisper encoder (VoiceEmbeddingPort.embed)."""
        embedding_port = StubEmbeddingPort()
        voice_repo = StubVoiceRepo(closest=None)
        use_case = IdentifyAgentByVoice(embedding_port, voice_repo)

        call = _make_call()
        await use_case.execute(call, audio_bytes=b"test-audio")

        assert embedding_port.call_count == 1

    async def test_voice_match_exact_threshold_accepted(self) -> None:
        """Граничный случай: score == threshold принимается."""
        embedding_port = StubEmbeddingPort()
        voice_repo = StubVoiceRepo(closest=(10, 0.85))
        use_case = IdentifyAgentByVoice(embedding_port, voice_repo, threshold=0.85)

        call = _make_call()
        result = await use_case.execute(call, audio_bytes=b"audio")

        assert result == 10
        assert call.detected_agent_id == 10

    async def test_no_transcription_no_regex_fallback(self) -> None:
        """Если транскрипции нет и score < threshold — None без ошибок."""
        embedding_port = StubEmbeddingPort()
        voice_repo = StubVoiceRepo(closest=(5, 0.5))
        use_case = IdentifyAgentByVoice(embedding_port, voice_repo, threshold=0.85)

        call = _make_call()  # no transcription
        result = await use_case.execute(call, audio_bytes=b"audio")

        assert result is None


# ============================================================
# MinIOAudioStorage
# ============================================================


class TestMinIOAudioStorage:
    async def test_upload_calls_put_object(self) -> None:
        from ..infrastructure.minio_adapter import MinIOAudioStorage

        minio_client = AsyncMock()
        minio_client.put_object = AsyncMock()
        storage = MinIOAudioStorage(minio_client, bucket="voice-samples")

        path = await storage.upload("calls/t2_001.ogg", b"audio-data")

        minio_client.put_object.assert_awaited_once()
        assert "calls/t2_001.ogg" in path

    async def test_get_presigned_url_returns_url(self) -> None:
        from ..infrastructure.minio_adapter import MinIOAudioStorage

        minio_client = AsyncMock()
        minio_client.presigned_get_object = AsyncMock(return_value="http://minio/url")
        storage = MinIOAudioStorage(minio_client)

        url = await storage.get_presigned_url("calls/t2_001.ogg")
        assert url == "http://minio/url"
