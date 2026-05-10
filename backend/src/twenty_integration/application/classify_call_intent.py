"""Stage-1 call-intent classifier.

Decides what to do with a freshly transcribed INCOMING call BEFORE we
create a Task in Twenty. The point: stop creating empty/duplicate Tasks
on calls that are not requests (acks, wrong-number, garbled STT) or that
are merely follow-ups on an already-open ticket.

The use case is asymmetric on purpose: missing a real request (FN) is far
more expensive than creating a junk Task (FP). So:
  - NO_ACTION accepted only with confidence ≥ 0.85 (and NEVER when AI
    looked uncertain). Anything weaker degrades to NEEDS_REVIEW.
  - UPDATE_EXISTING accepted only with confidence ≥ 0.7 AND the AI gave
    a parent_task_id that IS in the open-tickets set. Otherwise → NEW_TASK.
  - NEEDS_REVIEW always lands as a Task (kategoriya = «Требует разбора»).

Stage 0 is a tiny pre-AI heuristic for the trivial case `duration ≤ 5s`
— there is physically nothing to classify, so we short-circuit to
NO_ACTION without burning an AI call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# How far back we look for «open» tickets of the same client. Wider than
# DetectRepeat's 3-day window because an unresolved problem may legitimately
# stay open longer; assignees move slowly, customers re-call after holidays.
_OPEN_LOOKBACK = timedelta(days=14)

# Twenty Task.status values that count as «still open». Closed: VYPOLNENO,
# KORZINA. We don't include statusZayavki here — it's a softer domain
# status; the system status is a stricter open/closed signal.
_OPEN_STATUSES = {"TODO", "V_RABOTE"}

# Confidence thresholds. NEW_TASK has no threshold — it's the safe default.
_NO_ACTION_MIN_CONF = 0.85
_UPDATE_EXISTING_MIN_CONF = 0.70

# Stage-0 heuristic: calls shorter than this (in seconds) cannot carry
# a meaningful request. Even «удалите позицию» takes ~6 seconds to utter.
_MIN_DURATION_SEC = 5

KIND_NEW_TASK = "NEW_TASK"
KIND_UPDATE_EXISTING = "UPDATE_EXISTING"
KIND_NO_ACTION = "NO_ACTION"
KIND_NEEDS_REVIEW = "NEEDS_REVIEW"

_VALID_KINDS = {KIND_NEW_TASK, KIND_UPDATE_EXISTING, KIND_NO_ACTION, KIND_NEEDS_REVIEW}


class _IntentTwentyPort(Protocol):
    async def find_recent_tasks_by_caller_phone(
        self, caller_phone: str, since: datetime, limit: int = 10,
    ) -> list[dict[str, Any]]: ...
    async def find_call_records_by_task_id(
        self, task_id: str, *, direction: str | None = None,
    ) -> list[dict[str, Any]]: ...


class _IntentAIPort(Protocol):
    async def classify_call_intent(
        self, new_dialogue: str, open_tasks: list[dict[str, str]],
    ) -> dict[str, object]: ...


@dataclass
class IntentResult:
    """What the poller should do with this call.

    kind:
        NEW_TASK — create a fresh Task (default safe path).
        UPDATE_EXISTING — DON'T create a Task; mirror the CR onto
            `parent_task_id` (a currently-open ticket of this client).
        NO_ACTION — DON'T create a Task; CR still lands in Twenty without
            taskRel, so the call is visible in the calls feed.
        NEEDS_REVIEW — create a Task but force kategoriya='TREBUET_RAZBORA'
            and statusZayavki='TREBUETSYA_UTOCHNENIE' so an operator
            triages it manually.

    parent_task_id is non-None ONLY when kind == UPDATE_EXISTING.
    """
    kind: str = KIND_NEW_TASK
    parent_task_id: str | None = None
    confidence: float = 0.0
    reason: str = ""


def _heuristic_no_action(
    duration_sec: int | None, transcript: str,
) -> IntentResult | None:
    """Stage-0: cheap, certain-only filter. Returns None when AI must run."""
    if duration_sec is not None and duration_sec <= _MIN_DURATION_SEC:
        return IntentResult(
            kind=KIND_NO_ACTION,
            parent_task_id=None,
            confidence=1.0,
            reason=f"duration={duration_sec}s ≤ {_MIN_DURATION_SEC}s — heuristic",
        )
    # We deliberately do NOT add «short transcript» heuristics here:
    # «удалите позицию», «нет цены» are real requests under 100 chars.
    # Let the AI judge.
    return None


def _is_open(t: dict[str, Any]) -> bool:
    status = str(t.get("status") or "").upper()
    return status in _OPEN_STATUSES


class ClassifyCallIntent:
    """Decide intent of a new INCOMING call. See module docstring."""

    def __init__(
        self,
        twenty_port: _IntentTwentyPort,
        ai_port: _IntentAIPort | None,
        lookback: timedelta = _OPEN_LOOKBACK,
    ) -> None:
        self._twenty = twenty_port
        self._ai = ai_port
        self._lookback = lookback

    async def execute(
        self,
        *,
        transcript: str,
        client_phone: str | None,
        duration_sec: int | None = None,
    ) -> IntentResult:
        # Stage 0 — short-circuit truly empty calls.
        h = _heuristic_no_action(duration_sec, transcript or "")
        if h is not None:
            return h

        # Without AI we cannot judge — default to NEW_TASK so we don't
        # silently drop real requests.
        if self._ai is None:
            return IntentResult(
                kind=KIND_NEW_TASK, confidence=0.0,
                reason="no AI port — defaulting to NEW_TASK",
            )

        # Pull recent tickets and keep only the open ones.
        open_tasks: list[dict[str, Any]] = []
        if client_phone:
            try:
                since = datetime.now(UTC) - self._lookback
                recent = await self._twenty.find_recent_tasks_by_caller_phone(
                    client_phone, since, limit=10,
                )
                open_tasks = [t for t in recent if _is_open(t)]
            except Exception:
                logger.exception(
                    "classify_call_intent: open-tasks lookup failed phone=%s",
                    client_phone,
                )
                open_tasks = []

        # Build AI input: each open ticket carries its first INCOMING CR
        # transcript (the same enrichment DetectRepeat uses) so the AI can
        # judge semantic continuity, not just titles.
        ai_input: list[dict[str, str]] = []
        open_ids: set[str] = set()
        for t in open_tasks:
            tid = str(t.get("id") or "")
            if not tid:
                continue
            open_ids.add(tid)
            transcript_md = ""
            try:
                crs = await self._twenty.find_call_records_by_task_id(
                    tid, direction="INCOMING",
                )
                if crs:
                    tr = crs[0].get("transcript") or {}
                    if isinstance(tr, dict):
                        transcript_md = tr.get("markdown") or ""
            except Exception:
                logger.exception(
                    "classify_call_intent: prior CR fetch failed task=%s", tid,
                )
            ai_input.append({
                "id": tid,
                "title": str(t.get("title") or ""),
                "transcript": transcript_md,
            })

        try:
            raw = await self._ai.classify_call_intent(transcript or "", ai_input)
        except Exception:
            logger.exception("classify_call_intent: AI call raised")
            return IntentResult(
                kind=KIND_NEEDS_REVIEW, confidence=0.0,
                reason="AI raised — defaulting to NEEDS_REVIEW",
            )

        kind = str(raw.get("kind") or "").upper()
        if kind not in _VALID_KINDS:
            kind = KIND_NEEDS_REVIEW

        parent_raw = raw.get("parent_task_id")
        parent_id: str | None = None
        if parent_raw and str(parent_raw).strip().lower() not in {"null", "none", ""}:
            pid = str(parent_raw).strip()
            if pid in open_ids:
                parent_id = pid

        try:
            conf = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))

        reason = str(raw.get("reason") or "")[:300]

        # Apply asymmetric confidence thresholds.
        if kind == KIND_NO_ACTION and conf < _NO_ACTION_MIN_CONF:
            logger.info(
                "intent: NO_ACTION conf=%.2f < %.2f → degrading to NEEDS_REVIEW",
                conf, _NO_ACTION_MIN_CONF,
            )
            return IntentResult(
                kind=KIND_NEEDS_REVIEW, confidence=conf,
                reason=f"NO_ACTION confidence too low ({conf:.2f}); fallback. {reason}",
            )

        if kind == KIND_UPDATE_EXISTING:
            if parent_id is None:
                logger.info(
                    "intent: UPDATE_EXISTING without valid parent_id → NEW_TASK",
                )
                return IntentResult(
                    kind=KIND_NEW_TASK, confidence=conf,
                    reason=f"UPDATE_EXISTING but no valid parent. {reason}",
                )
            if conf < _UPDATE_EXISTING_MIN_CONF:
                logger.info(
                    "intent: UPDATE_EXISTING conf=%.2f < %.2f → NEW_TASK",
                    conf, _UPDATE_EXISTING_MIN_CONF,
                )
                return IntentResult(
                    kind=KIND_NEW_TASK, confidence=conf,
                    reason=(
                        f"UPDATE_EXISTING confidence too low ({conf:.2f}); "
                        f"safer to create new. {reason}"
                    ),
                )

        # NEW_TASK: parent_id is meaningless here.
        if kind == KIND_NEW_TASK:
            parent_id = None

        return IntentResult(
            kind=kind,
            parent_task_id=parent_id,
            confidence=conf,
            reason=reason,
        )
