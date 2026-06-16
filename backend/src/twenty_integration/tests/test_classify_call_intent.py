"""Tests for ClassifyCallIntent — the binary gate that decides whether
to create a Task for an INCOMING call or skip it as noise.

The use case enforces an asymmetric confidence threshold so that
losing real requests is harder than creating empty Tasks. Tests pin
the corner cases.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from twenty_integration.application.classify_call_intent import (
    KIND_NEW_TASK,
    KIND_NO_ACTION,
    ClassifyCallIntent,
)


def _ai(payload: dict[str, Any]) -> AsyncMock:
    ai = AsyncMock()
    ai.classify_call_intent = AsyncMock(return_value=payload)
    return ai


@pytest.mark.asyncio
async def test_stage0_short_duration_skips_ai():
    ai = _ai({})
    uc = ClassifyCallIntent(ai)

    result = await uc.execute(transcript="Алло.", duration_sec=3)

    assert result.kind == KIND_NO_ACTION
    # Heuristic must short-circuit before the AI call.
    ai.classify_call_intent.assert_not_called()


@pytest.mark.asyncio
async def test_stage0_threshold_inclusive():
    """duration == 5 still trips Stage-0; 6 does not."""
    ai = _ai({"is_actionable": True, "confidence": 0.9})

    res5 = await ClassifyCallIntent(ai).execute(transcript="Алло.", duration_sec=5)
    assert res5.kind == KIND_NO_ACTION
    ai.classify_call_intent.assert_not_called()

    res6 = await ClassifyCallIntent(ai).execute(transcript="Алло.", duration_sec=6)
    assert res6.kind == KIND_NEW_TASK
    ai.classify_call_intent.assert_called_once()


@pytest.mark.asyncio
async def test_not_actionable_above_threshold_skips_task():
    ai = _ai({
        "is_actionable": False,
        "confidence": 0.85,
        "reason": "garbled STT",
    })
    uc = ClassifyCallIntent(ai)

    result = await uc.execute(
        transcript="Афганистан? Афганистан?", duration_sec=15,
    )

    assert result.kind == KIND_NO_ACTION


@pytest.mark.asyncio
async def test_not_actionable_at_minimum_threshold_inclusive():
    """Threshold = 0.70: exactly 0.70 still counts as NO_ACTION."""
    ai = _ai({"is_actionable": False, "confidence": 0.70, "reason": "noise"})
    uc = ClassifyCallIntent(ai)

    result = await uc.execute(transcript="внутр. координация", duration_sec=20)
    assert result.kind == KIND_NO_ACTION


@pytest.mark.asyncio
async def test_not_actionable_below_threshold_falls_back_to_new_task():
    ai = _ai({
        "is_actionable": False,
        "confidence": 0.65,
        "reason": "weak guess",
    })
    uc = ClassifyCallIntent(ai)

    result = await uc.execute(
        transcript="что-то непонятное", duration_sec=15,
    )

    # Asymmetry: an uncertain «not a request» becomes a Task. Losing a
    # real request silently is the worst outcome we want to avoid.
    assert result.kind == KIND_NEW_TASK


@pytest.mark.asyncio
async def test_actionable_returns_new_task():
    ai = _ai({
        "is_actionable": True,
        "confidence": 0.92,
        "reason": "fresh request",
    })
    uc = ClassifyCallIntent(ai)

    result = await uc.execute(
        transcript="Удалите позицию из чека", duration_sec=12,
    )
    assert result.kind == KIND_NEW_TASK


@pytest.mark.asyncio
async def test_actionable_low_confidence_still_new_task():
    """Even shaky «yes, it's a task» wins — we never drop a request
    for low confidence on the actionable side."""
    ai = _ai({
        "is_actionable": True,
        "confidence": 0.30,
        "reason": "hard to tell but seems like a request",
    })
    uc = ClassifyCallIntent(ai)

    result = await uc.execute(transcript="что-то про настройку", duration_sec=20)
    assert result.kind == KIND_NEW_TASK


@pytest.mark.asyncio
async def test_ai_raises_falls_to_new_task():
    ai = AsyncMock()
    ai.classify_call_intent = AsyncMock(side_effect=RuntimeError("network"))
    uc = ClassifyCallIntent(ai)

    result = await uc.execute(transcript="что-то", duration_sec=20)

    # On AI failure: keep creating Tasks. Otherwise an AI outage would
    # mass-skip real requests for an hour.
    assert result.kind == KIND_NEW_TASK


@pytest.mark.asyncio
async def test_no_ai_port_defaults_to_new_task():
    uc = ClassifyCallIntent(ai_port=None)

    result = await uc.execute(transcript="что-то", duration_sec=20)

    assert result.kind == KIND_NEW_TASK


@pytest.mark.asyncio
async def test_unknown_payload_shape_is_treated_as_actionable():
    """Defensive: if AI returns a malformed dict (no is_actionable key),
    we MUST default to creating the Task — not silently swallowing it."""
    ai = _ai({"confidence": 0.9, "reason": "missing key"})
    uc = ClassifyCallIntent(ai)

    result = await uc.execute(transcript="реальный запрос", duration_sec=20)
    assert result.kind == KIND_NEW_TASK
