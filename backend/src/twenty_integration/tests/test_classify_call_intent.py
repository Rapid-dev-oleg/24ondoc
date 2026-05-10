"""Tests for ClassifyCallIntent — the Stage-1 intent gate that decides
whether to create a Task, attach to an existing one, drop the call, or
flag for manual review.

The use case mostly enforces asymmetric confidence thresholds and
filters out hallucinated parent ids, so the tests target those branches
explicitly. AI calls are stubbed; we feed back the exact dict the
adapter would return.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from twenty_integration.application.classify_call_intent import (
    KIND_NEEDS_REVIEW,
    KIND_NEW_TASK,
    KIND_NO_ACTION,
    KIND_UPDATE_EXISTING,
    ClassifyCallIntent,
)


def _ai_returning(payload: dict[str, Any]) -> AsyncMock:
    ai = AsyncMock()
    ai.classify_call_intent = AsyncMock(return_value=payload)
    return ai


def _twenty_with_open_tasks(open_tasks: list[dict[str, Any]]) -> AsyncMock:
    """Twenty stub returning a fixed list and empty CRs per task.

    `open_tasks` is what `find_recent_tasks_by_caller_phone` will hand back;
    the use case applies its own status filter, so include the status
    field in the fixtures (TODO/V_RABOTE = open).
    """
    twenty = AsyncMock()
    twenty.find_recent_tasks_by_caller_phone = AsyncMock(return_value=open_tasks)
    twenty.find_call_records_by_task_id = AsyncMock(return_value=[])
    return twenty


@pytest.mark.asyncio
async def test_stage0_short_duration_no_action_without_ai_call():
    ai = _ai_returning({})
    twenty = _twenty_with_open_tasks([])
    uc = ClassifyCallIntent(twenty, ai)

    result = await uc.execute(
        transcript="Алло.", client_phone="79991234567", duration_sec=3,
    )

    assert result.kind == KIND_NO_ACTION
    # Heuristic must short-circuit BEFORE calling AI.
    ai.classify_call_intent.assert_not_called()
    twenty.find_recent_tasks_by_caller_phone.assert_not_called()


@pytest.mark.asyncio
async def test_stage0_threshold_inclusive():
    """duration == 5s still trips Stage-0; 6s does not."""
    ai = _ai_returning({"kind": "NEW_TASK", "confidence": 0.9})
    twenty = _twenty_with_open_tasks([])

    res5 = await ClassifyCallIntent(twenty, ai).execute(
        transcript="Алло.", client_phone="79991234567", duration_sec=5,
    )
    assert res5.kind == KIND_NO_ACTION
    ai.classify_call_intent.assert_not_called()

    res6 = await ClassifyCallIntent(twenty, ai).execute(
        transcript="Алло.", client_phone="79991234567", duration_sec=6,
    )
    assert res6.kind == KIND_NEW_TASK
    ai.classify_call_intent.assert_called_once()


@pytest.mark.asyncio
async def test_no_action_high_confidence_accepted():
    ai = _ai_returning({
        "kind": "NO_ACTION",
        "confidence": 0.92,
        "parent_task_id": None,
        "reason": "garbled STT",
    })
    twenty = _twenty_with_open_tasks([])
    uc = ClassifyCallIntent(twenty, ai)

    result = await uc.execute(
        transcript="Афганистан? Афганистан?", client_phone="79991234567",
        duration_sec=15,
    )

    assert result.kind == KIND_NO_ACTION
    assert result.parent_task_id is None


@pytest.mark.asyncio
async def test_no_action_low_confidence_degrades_to_needs_review():
    ai = _ai_returning({
        "kind": "NO_ACTION",
        "confidence": 0.6,
        "parent_task_id": None,
        "reason": "unsure",
    })
    twenty = _twenty_with_open_tasks([])
    uc = ClassifyCallIntent(twenty, ai)

    result = await uc.execute(
        transcript="Алло, что-то там...", client_phone="79991234567",
        duration_sec=15,
    )

    # Critical asymmetry: an uncertain «not a task» MUST become a task
    # (NEEDS_REVIEW) so a real request never gets dropped silently.
    assert result.kind == KIND_NEEDS_REVIEW


@pytest.mark.asyncio
async def test_update_existing_accepted_with_valid_parent():
    ai = _ai_returning({
        "kind": "UPDATE_EXISTING",
        "confidence": 0.85,
        "parent_task_id": "open-1",
        "reason": "ack on prior request",
    })
    twenty = _twenty_with_open_tasks([
        {"id": "open-1", "title": "Нет интернета", "status": "V_RABOTE"},
    ])
    uc = ClassifyCallIntent(twenty, ai)

    result = await uc.execute(
        transcript="Алло, всё работает уже, спасибо",
        client_phone="79991234567", duration_sec=20,
    )

    assert result.kind == KIND_UPDATE_EXISTING
    assert result.parent_task_id == "open-1"


@pytest.mark.asyncio
async def test_update_existing_low_confidence_falls_back_to_new_task():
    ai = _ai_returning({
        "kind": "UPDATE_EXISTING",
        "confidence": 0.65,
        "parent_task_id": "open-1",
        "reason": "weak match",
    })
    twenty = _twenty_with_open_tasks([
        {"id": "open-1", "title": "Прошлая заявка", "status": "TODO"},
    ])
    uc = ClassifyCallIntent(twenty, ai)

    result = await uc.execute(
        transcript="что-то там про другое", client_phone="79991234567",
        duration_sec=20,
    )

    # Low-confidence linkage must NOT silently attach the call to an
    # existing ticket — that would hide a possibly-new request inside
    # an unrelated thread.
    assert result.kind == KIND_NEW_TASK
    assert result.parent_task_id is None


@pytest.mark.asyncio
async def test_update_existing_with_hallucinated_parent_falls_back_to_new_task():
    ai = _ai_returning({
        "kind": "UPDATE_EXISTING",
        "confidence": 0.95,
        "parent_task_id": "task-that-doesnt-exist",
        "reason": "hallucinated id",
    })
    twenty = _twenty_with_open_tasks([
        {"id": "open-1", "title": "Реальная задача", "status": "TODO"},
    ])
    uc = ClassifyCallIntent(twenty, ai)

    result = await uc.execute(
        transcript="продолжаем разговор", client_phone="79991234567",
        duration_sec=20,
    )

    # The AI named an id that's not in the open set: cannot trust it,
    # default to NEW_TASK.
    assert result.kind == KIND_NEW_TASK
    assert result.parent_task_id is None


@pytest.mark.asyncio
async def test_closed_tasks_filtered_out_of_open_set():
    """`find_recent_tasks_by_caller_phone` returns all recent tasks; the
    use case must keep only TODO/V_RABOTE in the open set fed to the AI
    AND when validating parent_task_id."""
    ai = _ai_returning({
        "kind": "UPDATE_EXISTING",
        "confidence": 0.95,
        "parent_task_id": "closed-1",  # closed → not in open set
        "reason": "linking to a closed ticket",
    })
    twenty = _twenty_with_open_tasks([
        {"id": "closed-1", "title": "Уже закрыта", "status": "VYPOLNENO"},
    ])
    uc = ClassifyCallIntent(twenty, ai)

    result = await uc.execute(
        transcript="что-то про прошлую закрытую", client_phone="79991234567",
        duration_sec=20,
    )

    assert result.kind == KIND_NEW_TASK
    # And the AI was given an empty open set (closed tickets stripped).
    args, kwargs = ai.classify_call_intent.call_args
    open_tasks_arg = args[1] if len(args) >= 2 else kwargs.get("open_tasks")
    assert open_tasks_arg == []


@pytest.mark.asyncio
async def test_new_task_passes_through():
    ai = _ai_returning({
        "kind": "NEW_TASK",
        "confidence": 0.9,
        "parent_task_id": None,
        "reason": "fresh request",
    })
    twenty = _twenty_with_open_tasks([])
    uc = ClassifyCallIntent(twenty, ai)

    result = await uc.execute(
        transcript="Удалите позицию из чека", client_phone="79991234567",
        duration_sec=12,
    )

    assert result.kind == KIND_NEW_TASK
    assert result.parent_task_id is None


@pytest.mark.asyncio
async def test_ai_raises_falls_to_needs_review():
    twenty = _twenty_with_open_tasks([])
    ai = AsyncMock()
    ai.classify_call_intent = AsyncMock(side_effect=RuntimeError("network"))
    uc = ClassifyCallIntent(twenty, ai)

    result = await uc.execute(
        transcript="что-то там", client_phone="79991234567", duration_sec=20,
    )

    # On AI failure the right side is «needs human review» — NOT skipping
    # the call (which would lose data) and NOT NEW_TASK (which would let
    # spam through if AI is down).
    assert result.kind == KIND_NEEDS_REVIEW


@pytest.mark.asyncio
async def test_no_ai_port_defaults_to_new_task():
    twenty = _twenty_with_open_tasks([])
    uc = ClassifyCallIntent(twenty, ai_port=None)

    result = await uc.execute(
        transcript="что-то", client_phone="79991234567", duration_sec=20,
    )

    # Without AI we cannot judge — safest is to keep creating tasks so
    # nothing gets lost; degenerates back to pre-Stage-1 behaviour.
    assert result.kind == KIND_NEW_TASK
    twenty.find_recent_tasks_by_caller_phone.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_kind_degrades_to_needs_review():
    ai = _ai_returning({
        "kind": "WHATEVER",
        "confidence": 0.99,
        "parent_task_id": None,
        "reason": "model hallucinated a new kind",
    })
    twenty = _twenty_with_open_tasks([])
    uc = ClassifyCallIntent(twenty, ai)

    result = await uc.execute(
        transcript="реальный запрос", client_phone="79991234567",
        duration_sec=20,
    )

    assert result.kind == KIND_NEEDS_REVIEW
