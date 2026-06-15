"""ATS2 poller — missed-call «Перезвонить» lifecycle.

Covers request 2: genuine missed incoming calls (ATS NOT_ANSWERED_COMMON)
spawn / dedup a «Перезвонить» task; the operator's own unanswered outbound
dials are ignored; an answered callback closes the task and (if a request
surfaced) spins a real заявка linked via parentTaskId.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ats_processing.application.ats2_poller import ATS2PollerService
from src.ats_processing.application.ats2_transcription_mapper import (
    ATS2TranscriptionMapper,
)
from src.twenty_integration.application.classify_call_intent import (
    KIND_NEW_TASK,
    KIND_NO_ACTION,
    IntentResult,
)
from src.twenty_integration.domain.models import TwentyTask
from src.twenty_integration.domain.ports import TwentyCRMPort


def _task(tid: str = "task-cb-1") -> TwentyTask:
    return TwentyTask(
        twenty_id=tid, title="", body="", status="TODO",
        due_at=None, assignee_id=None, person_id=None,
    )


def _twenty() -> Any:
    p = MagicMock(spec=TwentyCRMPort)
    p.find_open_callback_task_by_phone = AsyncMock(return_value=None)
    p.find_locations_by_phone = AsyncMock(return_value=[{"id": "loc-1"}])
    p.create_task = AsyncMock(return_value=_task())
    p.update_task_status = AsyncMock()
    p.update_task_parent = AsyncMock()
    p.find_recent_task_by_caller_phone = AsyncMock(return_value=None)
    return p


def _poller(twenty: Any, *, stt_text: str | None = None) -> ATS2PollerService:
    call_repo = MagicMock()
    call_repo.save = AsyncMock()
    call_repo.get_by_id = AsyncMock(return_value=None)

    ats2_client = MagicMock()
    ats2_client.download_recording = AsyncMock(return_value=b"x")

    stt_port = None
    if stt_text is not None:
        stt_port = MagicMock()
        stt_port.transcribe = AsyncMock(return_value=stt_text)

    sync_uc = MagicMock()
    sync_uc.execute = AsyncMock()

    poller = ATS2PollerService(
        ats2_client=ats2_client,
        call_repo=call_repo,
        transcription_mapper=ATS2TranscriptionMapper(),
        ai_port=MagicMock(),
        twenty_port=twenty,
        stt_port=stt_port,
        sync_call_uc=sync_uc,
    )
    return poller


def _raw(
    *,
    uuid: str = "ats-1",
    call_type: str = "SINGLE_CHANNEL",
    call_status: str = "NOT_ANSWERED_COMMON",
    caller: str = "79308003007",
    callee: str = "79616336451",
    record_file: str = "",
) -> dict[str, object]:
    raw: dict[str, object] = {
        "uuid": uuid,
        "callType": call_type,
        "callStatus": call_status,
        "callerNumber": caller,
        "calleeNumber": callee,
        "date": "2026-06-15T09:47:06.000000+03:00",
    }
    if record_file:
        raw["recordFileName"] = record_file
    return raw


@pytest.mark.asyncio
async def test_missed_incoming_creates_callback_task() -> None:
    twenty = _twenty()
    poller = _poller(twenty)

    await poller._process_new_call(_raw(), "ats-1")

    twenty.create_task.assert_awaited_once()
    kw = twenty.create_task.call_args.kwargs
    assert kw["is_missed_callback"] is True
    assert kw["istochnik"] == "ZVONOK"
    assert kw["caller_phone"] == "9308003007"
    assert kw["location_rel_id"] == "loc-1"
    assert kw["assignee_id"] is None  # «Перезвонить» без назначения
    assert "Перезвонить" in kw["title"]

    poller._sync_call_uc.execute.assert_awaited_once()
    skw = poller._sync_call_uc.execute.call_args.kwargs
    assert skw["task_id"] == "task-cb-1"
    assert skw["not_answered"] is True
    assert skw["direction"] == "INCOMING"


@pytest.mark.asyncio
async def test_serial_missed_calls_dedup_to_one_task() -> None:
    twenty = _twenty()
    poller = _poller(twenty)

    # Same client redials three times in one cycle.
    await poller._process_new_call(_raw(uuid="ats-1"), "ats-1")
    await poller._process_new_call(_raw(uuid="ats-2"), "ats-2")
    await poller._process_new_call(_raw(uuid="ats-3"), "ats-3")

    # Only the first spawns a task; the rest attach to it.
    twenty.create_task.assert_awaited_once()
    assert poller._sync_call_uc.execute.await_count == 3
    for call in poller._sync_call_uc.execute.call_args_list:
        assert call.kwargs["task_id"] == "task-cb-1"


@pytest.mark.asyncio
async def test_existing_open_callback_task_is_reused() -> None:
    twenty = _twenty()
    twenty.find_open_callback_task_by_phone = AsyncMock(
        return_value={"id": "task-existing", "status": "TODO"},
    )
    poller = _poller(twenty)

    await poller._process_new_call(_raw(), "ats-1")

    twenty.create_task.assert_not_called()
    skw = poller._sync_call_uc.execute.call_args.kwargs
    assert skw["task_id"] == "task-existing"


@pytest.mark.asyncio
async def test_outgoing_not_answered_is_ignored() -> None:
    twenty = _twenty()
    poller = _poller(twenty)

    await poller._process_new_call(
        _raw(call_type="OUTGOING", caller="79616336451", callee="79524679828"),
        "ats-1",
    )

    twenty.create_task.assert_not_called()
    poller._sync_call_uc.execute.assert_not_called()  # not mirrored
    # local record still persisted
    poller._call_repo.save.assert_awaited()


@pytest.mark.asyncio
async def test_missed_incoming_without_phone_mirrors_only() -> None:
    twenty = _twenty()
    poller = _poller(twenty)

    await poller._process_new_call(_raw(caller=""), "ats-1")

    twenty.create_task.assert_not_called()
    skw = poller._sync_call_uc.execute.call_args.kwargs
    assert skw["not_answered"] is True
    assert skw.get("task_id") is None


@pytest.mark.asyncio
async def test_answered_callback_closes_task_without_zayavka() -> None:
    twenty = _twenty()
    twenty.find_open_callback_task_by_phone = AsyncMock(
        return_value={"id": "cb-9", "status": "TODO"},
    )
    poller = _poller(twenty, stt_text="[Клиент]: да, уже всё решилось, спасибо")
    poller._classify_intent = MagicMock()
    poller._classify_intent.execute = AsyncMock(
        return_value=IntentResult(kind=KIND_NO_ACTION, confidence=0.9),
    )
    poller._create_task_from_call = AsyncMock()  # type: ignore[method-assign]

    await poller._process_new_call(
        _raw(call_type="OUTGOING", call_status="ANSWERED_COMMON",
             caller="79616336451", callee="79308003007", record_file="r.mp3"),
        "ats-1",
    )

    twenty.update_task_status.assert_awaited_once_with("cb-9", "VYPOLNENO")
    poller._create_task_from_call.assert_not_called()
    twenty.update_task_parent.assert_not_called()
    skw = poller._sync_call_uc.execute.call_args.kwargs
    assert skw["task_id"] == "cb-9"


@pytest.mark.asyncio
async def test_answered_callback_creates_and_links_zayavka() -> None:
    twenty = _twenty()
    twenty.find_open_callback_task_by_phone = AsyncMock(
        return_value={"id": "cb-9", "status": "TODO"},
    )
    poller = _poller(twenty, stt_text="[Клиент]: касса не печатает чеки")
    poller._classify_intent = MagicMock()
    poller._classify_intent.execute = AsyncMock(
        return_value=IntentResult(kind=KIND_NEW_TASK, confidence=0.95),
    )
    poller._create_task_from_call = AsyncMock(  # type: ignore[method-assign]
        return_value="zayavka-1",
    )

    await poller._process_new_call(
        _raw(call_type="OUTGOING", call_status="ANSWERED_COMMON",
             caller="79616336451", callee="79308003007", record_file="r.mp3"),
        "ats-1",
    )

    twenty.update_task_status.assert_awaited_once_with("cb-9", "VYPOLNENO")
    poller._create_task_from_call.assert_awaited_once()
    # callback stub linked to the real заявка
    twenty.update_task_parent.assert_awaited_once_with("cb-9", "zayavka-1")
    # successful call attaches to the заявка, not the closed stub
    skw = poller._sync_call_uc.execute.call_args.kwargs
    assert skw["task_id"] == "zayavka-1"
