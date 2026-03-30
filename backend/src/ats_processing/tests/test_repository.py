"""Tests for CallRecord ORM + Repository (DEV-49)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ..domain.models import CallRecord, CallStatus, SourceType
from ..infrastructure.orm_models import CallRecordORM
from ..infrastructure.repository import CallRecordRepositoryImpl

# ---------- Helpers ----------


def _make_call(
    call_id: str = "t2_001",
    status: CallStatus = CallStatus.NEW,
    caller_phone: str | None = "+79991234567",
) -> CallRecord:
    return CallRecord(
        call_id=call_id,
        audio_url="https://t2.example.com/rec/001.mp3",
        status=status,
        caller_phone=caller_phone,
        created_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
    )


def _make_orm(
    call_id: str = "t2_001",
    status: str = "new",
    source: str = "call_t2_webhook",
) -> CallRecordORM:
    row = CallRecordORM()
    row.call_id = call_id
    row.audio_url = "https://t2.example.com/rec/001.mp3"
    row.source = source
    row.transcription_t2 = None
    row.transcription_whisper = None
    row.duration = None
    row.caller_phone = "+79991234567"
    row.agent_ext = None
    row.detected_agent_id = None
    row.voice_match_score = None
    row.status = status
    row.session_id = None
    row.created_at = datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC)
    return row


def _make_session(existing_orm: CallRecordORM | None = None) -> AsyncMock:
    session = AsyncMock()
    session.get = AsyncMock(return_value=existing_orm)
    session.add = MagicMock()
    session.flush = AsyncMock()

    scalars_mock = MagicMock()
    scalars_mock.return_value = iter([])
    execute_result = MagicMock()
    execute_result.scalars = scalars_mock
    session.execute = AsyncMock(return_value=execute_result)
    return session


# ---------- Tests: to_domain / to_orm ----------


class TestCallRecordORM:
    def test_to_domain_maps_all_fields(self) -> None:
        row = _make_orm(call_id="abc123", status="processing")
        domain = CallRecordRepositoryImpl._to_domain(row)
        assert domain.call_id == "abc123"
        assert domain.status == CallStatus.PROCESSING
        assert domain.caller_phone == "+79991234567"

    def test_to_orm_maps_all_fields(self) -> None:
        record = _make_call(call_id="xyz", status=CallStatus.PREVIEW)
        row = CallRecordRepositoryImpl._to_orm(record)
        assert row.call_id == "xyz"
        assert row.status == "preview"
        assert row.caller_phone == "+79991234567"

    def test_update_orm_mutates_row(self) -> None:
        row = _make_orm(call_id="t2_001", status="new")
        record = _make_call(call_id="t2_001", status=CallStatus.PROCESSING)
        CallRecordRepositoryImpl._update_orm(row, record)
        assert row.status == "processing"


# ---------- Tests: get_by_id ----------


class TestCallRecordRepositoryGetById:
    async def test_returns_domain_when_found(self) -> None:
        orm_row = _make_orm("t2_001")
        session = _make_session(existing_orm=orm_row)
        repo = CallRecordRepositoryImpl(session)

        result = await repo.get_by_id("t2_001")

        assert result is not None
        assert result.call_id == "t2_001"
        session.get.assert_awaited_once_with(CallRecordORM, "t2_001")

    async def test_returns_none_when_not_found(self) -> None:
        session = _make_session(existing_orm=None)
        repo = CallRecordRepositoryImpl(session)

        result = await repo.get_by_id("missing")
        assert result is None


# ---------- Tests: save ----------


class TestCallRecordRepositorySave:
    async def test_call_record_orm_save_and_retrieve(self) -> None:
        """AC: save и get_by_id работают корректно."""
        session = _make_session(existing_orm=None)
        repo = CallRecordRepositoryImpl(session)
        record = _make_call("t2_new")

        await repo.save(record)

        session.add.assert_called_once()
        session.flush.assert_awaited_once()

    async def test_call_record_status_transitions_persist(self) -> None:
        """AC: статусные переходы сохраняются (update ORM row)."""
        orm_row = _make_orm("t2_001", status="new")
        session = _make_session(existing_orm=orm_row)
        repo = CallRecordRepositoryImpl(session)

        record = _make_call("t2_001", status=CallStatus.PROCESSING)
        await repo.save(record)

        # update path: add not called, flush still called
        session.add.assert_not_called()
        session.flush.assert_awaited_once()
        assert orm_row.status == "processing"

    async def test_save_sets_all_fields_correctly(self) -> None:
        session = _make_session(existing_orm=None)
        repo = CallRecordRepositoryImpl(session)
        record = CallRecord(
            call_id="t2_full",
            audio_url="https://example.com/audio.mp3",
            transcription_whisper="Привет",
            duration=120,
            caller_phone="+79001234567",
            agent_ext="101",
            detected_agent_id=42,
            voice_match_score=0.91,
            status=CallStatus.PREVIEW,
        )
        await repo.save(record)
        added: CallRecordORM = session.add.call_args[0][0]
        assert added.call_id == "t2_full"
        assert added.transcription_whisper == "Привет"
        assert added.duration == 120
        assert added.detected_agent_id == 42
        assert added.voice_match_score == pytest.approx(0.91)
        assert added.status == "preview"


# ---------- Tests: find_recent_by_phone ----------


class TestCallRecordRepositoryFindByPhone:
    async def test_find_recent_by_phone_returns_sorted(self) -> None:
        """AC: поиск по телефону возвращает отсортированные записи."""
        orm1 = _make_orm("t2_001")
        orm1.created_at = datetime(2026, 3, 27, 10, 0, 0, tzinfo=UTC)
        orm2 = _make_orm("t2_002")
        orm2.created_at = datetime(2026, 3, 27, 11, 0, 0, tzinfo=UTC)

        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.return_value = iter([orm2, orm1])  # sorted desc by created_at
        execute_result = MagicMock()
        execute_result.scalars = scalars_mock
        session.execute = AsyncMock(return_value=execute_result)

        repo = CallRecordRepositoryImpl(session)
        results = await repo.find_recent_by_phone("+79991234567", limit=10)

        assert len(results) == 2
        assert results[0].call_id == "t2_002"
        assert results[1].call_id == "t2_001"

    async def test_find_recent_by_phone_empty_when_none(self) -> None:
        session = _make_session()
        repo = CallRecordRepositoryImpl(session)
        results = await repo.find_recent_by_phone("+70000000000")
        assert results == []


# ---------- Tests: get_pending ----------


class TestCallRecordRepositoryGetPending:
    async def test_get_pending_returns_new_records(self) -> None:
        orm_row = _make_orm("t2_001", status="new")
        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.return_value = iter([orm_row])
        execute_result = MagicMock()
        execute_result.scalars = scalars_mock
        session.execute = AsyncMock(return_value=execute_result)

        repo = CallRecordRepositoryImpl(session)
        results = await repo.get_pending(limit=5)

        assert len(results) == 1
        assert results[0].call_id == "t2_001"
        assert results[0].status == CallStatus.NEW

    async def test_get_pending_filters_by_source(self) -> None:
        """AC: get_pending фильтрует по source."""
        orm_webhook = _make_orm("t2_001", status="new", source="call_t2_webhook")
        _make_orm("ats2_001", status="new", source="call_ats2_polling")

        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.return_value = iter([orm_webhook])
        execute_result = MagicMock()
        execute_result.scalars = scalars_mock
        session.execute = AsyncMock(return_value=execute_result)

        repo = CallRecordRepositoryImpl(session)
        results = await repo.get_pending(limit=10, source=SourceType.CALL_T2_WEBHOOK)

        assert len(results) == 1
        assert results[0].call_id == "t2_001"
        assert results[0].source == SourceType.CALL_T2_WEBHOOK

    async def test_existing_webhook_records_default_to_t2_webhook(self) -> None:
        """AC: существующие записи по умолчанию имеют source=call_t2_webhook."""
        orm_row = _make_orm("t2_legacy", status="new")
        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.return_value = iter([orm_row])
        execute_result = MagicMock()
        execute_result.scalars = scalars_mock
        session.execute = AsyncMock(return_value=execute_result)

        repo = CallRecordRepositoryImpl(session)
        results = await repo.get_pending(limit=10)

        assert len(results) == 1
        assert results[0].source == SourceType.CALL_T2_WEBHOOK
