"""Repeat detection based on prior tasks of the same Location.

A new task counts as a repeat (povtornoeObrashchenie=True) iff:
  1. We have a Location id, AND
  2. At least one prior task exists on that Location within the window, AND
  3. Either the new dialogue contains a trigger phrase (fast keyword path),
     OR the AI judges the new task to be semantically the same as one of
     the recent candidates (semantic path).

Returns the parent task id so the caller can set Task.parentTaskId.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

logger = logging.getLogger(__name__)

WINDOW_DAYS = 3

_KEYWORDS = [
    "повторное обращение", "опять", "снова", "та же проблема", "то же самое",
    "ещё раз", "еще раз", "не работает до сих пор", "всё ещё", "все еще",
    "не починили", "не исправили",
]
_KEYWORD_RE = re.compile(
    r"(?<![\wа-яё])(?:" + "|".join(re.escape(k) for k in _KEYWORDS) + r")(?![\wа-яё])",
    re.IGNORECASE,
)


class _RepeatPort(Protocol):
    async def find_recent_tasks_by_location_id(
        self, location_id: str, since: datetime, limit: int = 10,
    ) -> list[dict[str, Any]]: ...
    async def find_recent_tasks_by_caller_phone(
        self, caller_phone: str, since: datetime, limit: int = 10,
    ) -> list[dict[str, Any]]: ...
    async def find_call_records_by_task_id(
        self, task_id: str, *, direction: str | None = None,
    ) -> list[dict[str, Any]]: ...


class _RepeatAIPort(Protocol):
    async def check_repeat_status(
        self, new_text: str, recent_tasks: list[dict[str, str]],
    ) -> dict[str, object]: ...


# Chain-position values used in Twenty (Task.obrashchenieKind, CallRecord.callKind)
KIND_NEW = "NEW"
KIND_REPEAT = "REPEAT"
KIND_SYSTEM = "SYSTEM"


@dataclass
class RepeatResult:
    """Result of repeat detection.

    chain_position: NEW (1st in chain) | REPEAT (2nd) | SYSTEM (3+).
    is_repeat: legacy bool, derived (NEW=False, REPEAT/SYSTEM=True).
    parent_task_id: most-recent prior task in the chain (None for NEW).
    """
    chain_position: str = KIND_NEW
    is_repeat: bool = False
    parent_task_id: str | None = None
    match_reason: str = "none"  # "keyword" | "semantic" | "none"
    recent_candidates: int = 0


def _kind_from_count(prior_count: int) -> str:
    if prior_count <= 0:
        return KIND_NEW
    if prior_count == 1:
        return KIND_REPEAT
    return KIND_SYSTEM


def _result(chain_position: str, parent_id: str | None,
            reason: str, count: int) -> RepeatResult:
    return RepeatResult(
        chain_position=chain_position,
        is_repeat=chain_position in (KIND_REPEAT, KIND_SYSTEM),
        parent_task_id=parent_id,
        match_reason=reason,
        recent_candidates=count,
    )


class DetectRepeat:
    """Determine the chain position of a new ticket within a 3-day window.

    Candidates set: tasks of the same client_phone + tasks of the same
    location_id (deduped). On the candidates we run a keyword check
    against the new dialogue (operator's «опять / то же / не работает
    до сих пор / ещё раз») — a chain step is only valid if the new
    dialogue actually references prior contact. Without keywords every
    second call from the same number would falsely become REPEAT.

    On a positive keyword hit, chain length = number of distinct prior
    candidates that survived dedup (capped at 10):
      0 → NEW (no priors at all)
      1 → REPEAT (one prior — this is the second contact)
      2+ → SYSTEM (problem hasn't been closed across multiple attempts).
    """

    def __init__(
        self,
        twenty_port: _RepeatPort,
        ai_port: _RepeatAIPort | None,
        window: timedelta = timedelta(days=WINDOW_DAYS),
    ) -> None:
        self._twenty = twenty_port
        self._ai = ai_port
        self._window = window

    async def execute(
        self,
        *,
        location_id: str | None = None,
        client_phone: str | None = None,
        new_dialogue: str = "",
    ) -> RepeatResult:
        if not location_id and not client_phone:
            return _result(KIND_NEW, None, "none", 0)

        since = datetime.now(UTC) - self._window
        # Pull candidates from both axes; dedupe by id.
        candidates: dict[str, dict[str, Any]] = {}
        if client_phone:
            try:
                by_phone = await self._twenty.find_recent_tasks_by_caller_phone(
                    client_phone, since, limit=10,
                )
                for t in by_phone:
                    tid = str(t.get("id") or "")
                    if tid:
                        candidates[tid] = t
            except Exception:
                logger.exception("by_caller_phone candidates failed")
        if location_id:
            try:
                by_loc = await self._twenty.find_recent_tasks_by_location_id(
                    location_id, since, limit=10,
                )
                for t in by_loc:
                    tid = str(t.get("id") or "")
                    if tid and tid not in candidates:
                        candidates[tid] = t
            except Exception:
                logger.exception("by_location_id candidates failed")

        if not candidates:
            return _result(KIND_NEW, None, "none", 0)

        # Sort newest-first so parent = most recent prior task
        recent = sorted(
            candidates.values(),
            key=lambda r: r.get("createdAt") or "",
            reverse=True,
        )

        # Keyword fast path — operator referenced previous contact.
        if _KEYWORD_RE.search(new_dialogue or ""):
            kind = _kind_from_count(len(recent))
            return _result(
                kind,
                str(recent[0].get("id") or "") or None,
                "keyword",
                len(recent),
            )

        if self._ai is None:
            return _result(KIND_NEW, None, "none", len(recent))

        # Semantic path — feed AI the FIRST INCOMING CR transcript per
        # prior task instead of just title/description. The transcript
        # carries the actual problem the customer described, which is
        # what we want to match against the new dialogue.
        ai_input: list[dict[str, str]] = []
        for r in recent:
            tid = str(r.get("id") or "")
            transcript = ""
            try:
                crs = await self._twenty.find_call_records_by_task_id(
                    tid, direction="INCOMING",
                )
                if crs:
                    tr = crs[0].get("transcript") or {}
                    if isinstance(tr, dict):
                        transcript = tr.get("markdown") or ""
            except Exception:
                logger.exception("loading prior CR transcripts failed task=%s", tid)
            ai_input.append({
                "id": tid,
                "title": str(r.get("title") or ""),
                "description": _body_markdown(r.get("bodyV2")) or "",
                "transcript": transcript,
            })
        try:
            ai_out = await self._ai.check_repeat_status(new_dialogue or "", ai_input)
        except Exception:
            logger.exception("check_repeat_status call failed")
            return _result(KIND_NEW, None, "none", len(recent))

        matches = list(ai_out.get("matches") or [])
        if not matches:
            return _result(KIND_NEW, None, "none", len(recent))

        matched_ids = {str(m) for m in matches}
        parent_id = next(
            (str(r["id"]) for r in recent if str(r.get("id") or "") in matched_ids),
            str(recent[0].get("id") or "") or None,
        )
        kind = _kind_from_count(len(recent))
        return _result(kind, parent_id, "semantic", len(recent))


def _body_markdown(body: Any) -> str | None:
    """Twenty bodyV2 is {blocknote, markdown}; pull plain text safely."""
    if isinstance(body, dict):
        md = body.get("markdown")
        if isinstance(md, str):
            return md
    return None
