"""Tests for ATS Processing domain models."""

import pytest

from ..domain.models import CallRecord, CallStatus, SourceType


def make_call(call_id: str = "t2_001") -> CallRecord:
    return CallRecord(call_id=call_id, audio_url="https://t2.example.com/rec/001.mp3")


class TestCallRecord:
    def test_initial_status_is_new(self) -> None:
        call = make_call()
        assert call.status == CallStatus.NEW

    def test_start_processing(self) -> None:
        call = make_call()
        call.start_processing()
        assert call.status == CallStatus.PROCESSING

    def test_start_processing_fails_if_not_new(self) -> None:
        call = make_call()
        call.start_processing()
        with pytest.raises(ValueError):
            call.start_processing()

    def test_set_transcription_whisper(self) -> None:
        call = make_call()
        call.set_transcription("Привет, у меня проблема с заказом", "whisper")
        assert call.transcription_whisper == "Привет, у меня проблема с заказом"

    def test_set_transcription_t2(self) -> None:
        call = make_call()
        call.set_transcription("Тест т2", "t2")
        assert call.transcription_t2 == "Тест т2"

    def test_get_best_transcription_prefers_whisper(self) -> None:
        call = make_call()
        call.set_transcription("t2 text", "t2")
        call.set_transcription("whisper text", "whisper")
        assert call.get_best_transcription() == "whisper text"

    def test_get_best_transcription_falls_back_to_t2(self) -> None:
        call = make_call()
        call.set_transcription("t2 text", "t2")
        assert call.get_best_transcription() == "t2 text"

    def test_set_voice_match_valid(self) -> None:
        call = make_call()
        call.set_voice_match(agent_id=42, score=0.92)
        assert call.detected_agent_id == 42
        assert call.voice_match_score == pytest.approx(0.92)

    def test_set_voice_match_invalid_score(self) -> None:
        call = make_call()
        with pytest.raises(ValueError, match="between 0 and 1"):
            call.set_voice_match(agent_id=42, score=1.5)

    def test_mark_created_requires_preview(self) -> None:
        call = make_call()
        with pytest.raises(ValueError, match="preview state"):
            call.mark_created()

    def test_call_record_stores_source_type(self) -> None:
        """AC: CallRecord хранит source_type с дефолтом call_t2_webhook."""
        call = make_call()
        assert call.source == SourceType.CALL_T2_WEBHOOK

        call_polling = CallRecord(
            call_id="ats2_001",
            audio_url="https://example.com/rec.mp3",
            source=SourceType.CALL_ATS2_POLLING,
        )
        assert call_polling.source == SourceType.CALL_ATS2_POLLING
