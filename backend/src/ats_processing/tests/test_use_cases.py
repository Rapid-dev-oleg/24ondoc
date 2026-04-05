"""Tests for ATS Processing use cases (DEV-52)."""

from __future__ import annotations

import pytest

from ..application.ports import VoiceEmbeddingPort
from ..application.use_cases import (
    EnrollVoiceSampleUseCase,
    IdentifyAgentByVoice,
)
from ..domain.models import CallRecord
from ..domain.repository import AgentVoiceSampleRepository, CallRecordRepository

# ---------- Stubs ----------


class StubCallRepo(CallRecordRepository):
    def __init__(self) -> None:
        self.saved: list[CallRecord] = []

    async def get_by_id(self, call_id: str) -> CallRecord | None:
        return next((r for r in self.saved if r.call_id == call_id), None)

    async def save(self, record: CallRecord) -> None:
        self.saved.append(record)

    async def get_pending(
        self, limit: int = 10, source: object | None = None
    ) -> list[CallRecord]:
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
        self.saved: list[tuple[int, list[float]]] = []

    async def find_closest(self, embedding: list[float]) -> tuple[int, float] | None:
        return self._closest

    async def save(self, agent_id: int, embedding: list[float]) -> None:
        self.saved.append((agent_id, embedding))


def _make_call(call_id: str = "t2_001") -> CallRecord:
    return CallRecord(
        call_id=call_id,
        audio_url="https://t2.example.com/rec/001.mp3",
    )


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
# EnrollVoiceSampleUseCase
# ============================================================


class FailingEmbeddingPort(VoiceEmbeddingPort):
    async def embed(self, audio_bytes: bytes) -> list[float]:
        raise RuntimeError("embedding service unavailable")


class FailingSaveVoiceRepo(AgentVoiceSampleRepository):
    async def find_closest(self, embedding: list[float]) -> tuple[int, float] | None:
        return None

    async def save(self, agent_id: int, embedding: list[float]) -> None:
        raise RuntimeError("db unavailable")


class TestEnrollVoiceSampleUseCase:
    async def test_success_returns_true_and_saves_embedding(self) -> None:
        embedding_port = StubEmbeddingPort(embedding=[0.5] * 384)
        voice_repo = StubVoiceRepo()
        uc = EnrollVoiceSampleUseCase(embedding_port, voice_repo)

        result = await uc.execute(agent_id=7, audio_bytes=b"audio")

        assert result is True
        assert len(voice_repo.saved) == 1
        assert voice_repo.saved[0][0] == 7
        assert voice_repo.saved[0][1] == [0.5] * 384

    async def test_embedding_error_returns_false(self) -> None:
        embedding_port = FailingEmbeddingPort()
        voice_repo = StubVoiceRepo()
        uc = EnrollVoiceSampleUseCase(embedding_port, voice_repo)

        result = await uc.execute(agent_id=1, audio_bytes=b"audio")

        assert result is False
        assert len(voice_repo.saved) == 0

    async def test_save_error_returns_false(self) -> None:
        embedding_port = StubEmbeddingPort()
        voice_repo = FailingSaveVoiceRepo()
        uc = EnrollVoiceSampleUseCase(embedding_port, voice_repo)

        result = await uc.execute(agent_id=2, audio_bytes=b"audio")

        assert result is False
