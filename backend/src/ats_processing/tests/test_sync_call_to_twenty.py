"""SyncCallToTwentyUseCase: mirror local ats_call_records into Twenty.

Tests the idempotent upsert + phone-based Location attach. Person-as-
client-bucket is gone — caller phone now sits on CallRecord.callerPhone,
callee on CallRecord.calleePhone, and the link to the outlet is just
CallRecord.locationRelId.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ats_processing.application.sync_call_to_twenty import SyncCallToTwentyUseCase
from src.ats_processing.domain.models import CallRecord, CallStatus, SourceType
from src.twenty_integration.domain.ports import TwentyCRMPort


def _call(
    call_id: str = "ats-42",
    phone: str | None = "79063567906",
    status: CallStatus = CallStatus.CREATED,
    transcript: str | None = "Алло. Аполло 32.",
) -> CallRecord:
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
    # Catalog is read-only — we only attach by phone, never create.
    p.find_locations_by_phone = AsyncMock(return_value=[{"id": "loc-7"}])
    p.create_call_record = AsyncMock(return_value={"id": "call-twenty-7"})
    p.update_call_record = AsyncMock()
    p.update_call_record_script_check = AsyncMock()
    p.recompute_task_aggregates = AsyncMock()
    p.find_operator_by_phone = AsyncMock(return_value=None)
    p.get_task = AsyncMock(return_value=None)
    return p


@pytest.mark.asyncio
async def test_new_call_with_phone_creates_twenty_call_record() -> None:
    port = _port()
    uc = SyncCallToTwentyUseCase(twenty_port=port)

    result = await uc.execute(
        _call(), task_id="task-123", callee_phone="79049042060",
    )

    port.create_call_record.assert_awaited_once()
    kwargs = port.create_call_record.call_args.kwargs
    assert kwargs["caller_phone"] == "79063567906"
    assert kwargs["callee_phone"] == "79049042060"
    assert kwargs["call_status"] == "ANSWERED"
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

    port.find_locations_by_phone.assert_not_called()
    assert result.created is True
    kwargs = port.create_call_record.call_args.kwargs
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
        "locationRelId": "l",
        "transcript": {"markdown": "..."},
        "calleePhone": {"primaryPhoneNumber": "9049042060"},
    }
    uc = SyncCallToTwentyUseCase(twenty_port=port)

    result = await uc.execute(_call(), task_id="task-999")

    port.create_call_record.assert_not_called()
    port.update_call_record.assert_awaited_once()
    kwargs = port.update_call_record.call_args.kwargs
    assert kwargs["task_rel_id"] == "task-999"
    # existing location should not be overwritten
    assert kwargs["location_rel_id"] is None
    assert kwargs["transcript"] is None  # already has one
    # existing callee not overwritten either
    assert kwargs["callee_phone"] is None
    assert result.created is False
    assert result.twenty_id == "call-twenty-existing"


@pytest.mark.asyncio
async def test_existing_record_gets_transcript_when_previously_empty() -> None:
    port = _port()
    port.find_call_record_by_ats_id.return_value = {
        "id": "cid", "locationRelId": None, "transcript": None,
    }
    uc = SyncCallToTwentyUseCase(twenty_port=port)

    await uc.execute(_call(transcript="Новая транскрипция"))

    kwargs = port.update_call_record.call_args.kwargs
    assert kwargs["transcript"] == "Новая транскрипция"


@pytest.mark.asyncio
async def test_existing_record_gets_callee_when_previously_empty() -> None:
    port = _port()
    port.find_call_record_by_ats_id.return_value = {
        "id": "cid",
        "locationRelId": "l",
        "transcript": {"markdown": "..."},
        # No calleePhone yet — let it backfill on the next sync.
    }
    uc = SyncCallToTwentyUseCase(twenty_port=port)

    await uc.execute(_call(), task_id="task-1", callee_phone="79049042060")

    kwargs = port.update_call_record.call_args.kwargs
    assert kwargs["callee_phone"] == "79049042060"


@pytest.mark.asyncio
async def test_phone_resolve_failure_does_not_abort_sync() -> None:
    port = _port()
    port.find_locations_by_phone.side_effect = RuntimeError("twenty down")
    uc = SyncCallToTwentyUseCase(twenty_port=port)

    result = await uc.execute(_call())

    # Still syncs the call, just without a Location relation
    port.create_call_record.assert_awaited_once()
    kwargs = port.create_call_record.call_args.kwargs
    assert kwargs.get("location_rel_id") is None
    assert result.created is True


def _existing_call_record() -> dict[str, Any]:
    return {
        "id": "call-twenty-1",
        "locationRelId": "l",
        "transcript": {"markdown": "..."},
    }


@pytest.mark.asyncio
async def test_script_check_writes_russian_phrases_not_ids() -> None:
    port = _port()
    port.find_call_record_by_ats_id.return_value = _existing_call_record()
    script_ai = MagicMock()
    script_ai.check_script = AsyncMock(return_value={
        "missing": ["greeting", "farewell"],
        "violations_count": 2,
    })
    uc = SyncCallToTwentyUseCase(twenty_port=port, script_ai=script_ai)

    await uc.execute(_call(transcript="[Оператор]: алло"), task_id="t-1")

    # Script result lands on the CallRecord (not Task) now
    port.update_call_record_script_check.assert_awaited_once()
    args = port.update_call_record_script_check.call_args.args
    assert args[0] == "call-twenty-1"  # the Twenty CR id, not task id
    assert args[1] == 2
    assert args[2] == [
        "Здравствуйте, чем я вам могу помочь",
        "До свидания",
    ]
    # And task aggregates are recomputed afterwards
    port.recompute_task_aggregates.assert_awaited_once_with("t-1")


@pytest.mark.asyncio
async def test_script_check_unknown_id_passes_through_verbatim() -> None:
    port = _port()
    port.find_call_record_by_ats_id.return_value = _existing_call_record()
    script_ai = MagicMock()
    script_ai.check_script = AsyncMock(return_value={
        "missing": ["mystery_phrase"],
        "violations_count": 1,
    })
    uc = SyncCallToTwentyUseCase(twenty_port=port, script_ai=script_ai)

    await uc.execute(_call(transcript="..."), task_id="t-2")

    args = port.update_call_record_script_check.call_args.args
    assert args[2] == ["mystery_phrase"]


@pytest.mark.asyncio
async def test_call_kind_resolved_from_task() -> None:
    port = _port()
    port.get_task = AsyncMock(return_value={"povtornoeObrashchenie": True})
    uc = SyncCallToTwentyUseCase(twenty_port=port)

    await uc.execute(_call(), task_id="t-repeat",
                     callee_phone="79049042060")

    kwargs = port.create_call_record.call_args.kwargs
    assert kwargs["call_kind"] == "REPEAT"


@pytest.mark.asyncio
async def test_operator_resolved_from_callee_phone() -> None:
    port = _port()
    port.find_operator_by_phone = AsyncMock(
        return_value={"id": "op-42", "displayName": "Оператор Иван"}
    )
    uc = SyncCallToTwentyUseCase(twenty_port=port)

    await uc.execute(_call(), task_id="t-1", callee_phone="79049042060")

    port.find_operator_by_phone.assert_awaited_once_with("79049042060")
    kwargs = port.create_call_record.call_args.kwargs
    assert kwargs["operator_rel_id"] == "op-42"
