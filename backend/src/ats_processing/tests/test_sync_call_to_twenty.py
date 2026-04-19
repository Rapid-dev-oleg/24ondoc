"""Stage 4 — SyncCallToTwentyUseCase: mirror local ats_call_records into Twenty.

Exercises the idempotent upsert + relation resolution with a mock adapter.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ats_processing.application.sync_call_to_twenty import SyncCallToTwentyUseCase
from src.ats_processing.domain.models import CallRecord, CallStatus, SourceType
from src.twenty_integration.domain.ports import TwentyCRMPort


def _call(call_id: str = "ats-42", phone: str | None = "79063567906",
          status: CallStatus = CallStatus.CREATED,
          transcript: str | None = "Алло. Аполло 32.") -> CallRecord:
    return CallRecord(
        call_id=call_id,
        audio_url="https://a/x.ogg",
        source=SourceType.CALL_ATS2_POLLING,
        transcription_whisper=transcript,
        duration=60,
        caller_phone=phone,
        status=status,
        created_at=datetime(2026, 4, 1, 12, 0, 0),
    )


def _port() -> Any:
    p = MagicMock(spec=TwentyCRMPort)
    p.find_call_record_by_ats_id = AsyncMock(return_value=None)
    p.find_person_by_phone = AsyncMock(return_value=None)
    p.create_person_with_phone = AsyncMock(return_value={"id": "person-7"})
    p.find_location_by_phone = AsyncMock(return_value=None)
    p.create_location = AsyncMock(return_value={"id": "loc-7"})
    p.create_call_record = AsyncMock(return_value={"id": "call-twenty-7"})
    p.update_call_record = AsyncMock()
    return p


@pytest.mark.asyncio
async def test_new_call_with_phone_creates_twenty_call_record() -> None:
    port = _port()
    uc = SyncCallToTwentyUseCase(twenty_port=port)

    result = await uc.execute(_call(), task_id="task-123")

    port.create_call_record.assert_awaited_once()
    kwargs = port.create_call_record.call_args.kwargs
    assert kwargs["caller_phone"] == "79063567906"
    assert kwargs["call_status"] == "ANSWERED"
    assert kwargs["person_rel_id"] == "person-7"
    assert kwargs["location_rel_id"] == "loc-7"
    assert kwargs["task_rel_id"] == "task-123"
    assert result.created is True
    assert result.linked_task is True
    assert result.twenty_id == "call-twenty-7"


@pytest.mark.asyncio
async def test_new_call_without_phone_still_syncs_without_relations() -> None:
    port = _port()
    uc = SyncCallToTwentyUseCase(twenty_port=port)

    result = await uc.execute(_call(phone=None))

    port.find_person_by_phone.assert_not_called()
    port.find_location_by_phone.assert_not_called()
    assert result.created is True
    kwargs = port.create_call_record.call_args.kwargs
    assert kwargs.get("person_rel_id") is None
    assert kwargs.get("location_rel_id") is None


@pytest.mark.asyncio
async def test_missed_call_maps_status_to_missed() -> None:
    port = _port()
    uc = SyncCallToTwentyUseCase(twenty_port=port)

    await uc.execute(_call(status=CallStatus.NEW))

    kwargs = port.create_call_record.call_args.kwargs
    assert kwargs["call_status"] == "MISSED"


@pytest.mark.asyncio
async def test_existing_record_is_patched_with_task_id_not_recreated() -> None:
    port = _port()
    port.find_call_record_by_ats_id.return_value = {
        "id": "call-twenty-existing",
        "personRelId": "p",
        "locationRelId": "l",
        "transcript": {"markdown": "..."},
    }
    uc = SyncCallToTwentyUseCase(twenty_port=port)

    result = await uc.execute(_call(), task_id="task-999")

    port.create_call_record.assert_not_called()
    port.update_call_record.assert_awaited_once()
    kwargs = port.update_call_record.call_args.kwargs
    assert kwargs["task_rel_id"] == "task-999"
    # existing person/location should not be overwritten
    assert kwargs["person_rel_id"] is None
    assert kwargs["location_rel_id"] is None
    assert kwargs["transcript"] is None  # already has one
    assert result.created is False
    assert result.twenty_id == "call-twenty-existing"


@pytest.mark.asyncio
async def test_existing_record_gets_transcript_when_previously_empty() -> None:
    port = _port()
    port.find_call_record_by_ats_id.return_value = {
        "id": "cid", "personRelId": None, "locationRelId": None, "transcript": None,
    }
    uc = SyncCallToTwentyUseCase(twenty_port=port)

    await uc.execute(_call(transcript="Новая транскрипция"))

    kwargs = port.update_call_record.call_args.kwargs
    assert kwargs["transcript"] == "Новая транскрипция"


@pytest.mark.asyncio
async def test_person_lookup_failure_does_not_abort_sync() -> None:
    port = _port()
    port.find_person_by_phone.side_effect = RuntimeError("twenty down")
    uc = SyncCallToTwentyUseCase(twenty_port=port)

    result = await uc.execute(_call())

    # Still syncs the call, just without relations
    port.create_call_record.assert_awaited_once()
    kwargs = port.create_call_record.call_args.kwargs
    assert kwargs.get("person_rel_id") is None
    assert kwargs.get("location_rel_id") is None
    assert result.created is True
