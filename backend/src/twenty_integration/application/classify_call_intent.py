"""Binary intent gate for INCOMING calls.

One question only: is this call a meaningful customer request, or is it
noise (failed connection, garbled STT, "Алло./Спасибо.", wrong number,
internal coordination)? If we are SURE it's noise — skip Task creation
and let the CallRecord live in the calls feed without taskRel.
Anything else — create the Task as usual; downstream code (DetectRepeat,
category selection, etc.) handles the rest.

Asymmetry: missing a real request (FN) is far worse than creating an
empty Task (FP). So NO_ACTION is accepted only at confidence ≥ 0.85;
any sliver of doubt → create the Task.

Stage 0 is a tiny duration-based short-circuit. Stage 1 is the AI call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

# Confidence below which an AI «not actionable» verdict is rejected and
# we fall back to creating the Task — the cost of one extra empty Task
# is tiny compared to losing a real request. Tuned to 0.70 because the
# binary prompt asks ONE objective question (is there a customer
# request?) so anything ≥ 0.70 is meaningful conviction; raising to
# 0.85 left obvious noise tasks (internal coordination, ack-only chat)
# in the active feed in our 2026-05 backfill.
_NO_ACTION_MIN_CONF = 0.70

# Stage-0 heuristic: anything ≤ this is physically too short to carry a
# meaningful request. Even «удалите позицию» takes ~6 seconds to utter.
_MIN_DURATION_SEC = 5

KIND_NEW_TASK = "NEW_TASK"
KIND_NO_ACTION = "NO_ACTION"


class _IntentAIPort(Protocol):
    async def classify_call_intent(
        self, new_dialogue: str,
    ) -> dict[str, object]: ...


@dataclass
class IntentResult:
    """Bool-ish verdict: should the poller create a Task?

    kind:
        NEW_TASK   — proceed with the usual create_task path.
        NO_ACTION  — skip create_task. CR still mirrors into Twenty,
                     just without taskRel.
    """
    kind: str = KIND_NEW_TASK
    confidence: float = 0.0
    reason: str = ""


class ClassifyCallIntent:
    """Decide whether a new INCOMING call deserves a Task. See module doc."""

    def __init__(self, ai_port: _IntentAIPort | None) -> None:
        self._ai = ai_port

    async def execute(
        self,
        *,
        transcript: str,
        duration_sec: int | None = None,
    ) -> IntentResult:
        # Stage 0 — definitely-empty calls.
        if duration_sec is not None and duration_sec <= _MIN_DURATION_SEC:
            return IntentResult(
                kind=KIND_NO_ACTION,
                confidence=1.0,
                reason=(
                    f"duration={duration_sec}s ≤ {_MIN_DURATION_SEC}s — heuristic"
                ),
            )

        # Without an AI port we cannot judge — keep creating Tasks so
        # nothing real gets dropped silently.
        if self._ai is None:
            return IntentResult(
                kind=KIND_NEW_TASK, confidence=0.0,
                reason="no AI port — defaulting to NEW_TASK",
            )

        try:
            raw = await self._ai.classify_call_intent(transcript or "")
        except Exception:
            logger.exception("classify_call_intent: AI call raised")
            return IntentResult(
                kind=KIND_NEW_TASK, confidence=0.0,
                reason="AI raised — defaulting to NEW_TASK",
            )

        is_actionable = bool(raw.get("is_actionable", True))
        try:
            conf = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        reason = str(raw.get("reason") or "")[:300]

        # Only commit to NO_ACTION when the model is loud about it.
        # Anything softer falls back to NEW_TASK by design.
        if not is_actionable and conf >= _NO_ACTION_MIN_CONF:
            return IntentResult(
                kind=KIND_NO_ACTION, confidence=conf, reason=reason,
            )

        if not is_actionable:
            logger.info(
                "intent: not_actionable but conf=%.2f < %.2f → NEW_TASK",
                conf, _NO_ACTION_MIN_CONF,
            )
            return IntentResult(
                kind=KIND_NEW_TASK, confidence=conf,
                reason=f"low-confidence not-a-task ({conf:.2f}); creating Task. {reason}",
            )

        return IntentResult(
            kind=KIND_NEW_TASK, confidence=conf, reason=reason,
        )
