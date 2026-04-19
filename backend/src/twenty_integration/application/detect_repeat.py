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


class _LocationPort(Protocol):
    async def find_recent_tasks_by_location_id(
        self, location_id: str, since: datetime, limit: int = 10
    ) -> list[dict[str, Any]]: ...


class _RepeatAIPort(Protocol):
    async def check_repeat_status(
        self, new_text: str, recent_tasks: list[dict[str, str]],
    ) -> dict[str, object]: ...


@dataclass
class RepeatResult:
    is_repeat: bool
    parent_task_id: str | None
    match_reason: str  # "keyword" | "semantic" | "none"
    recent_candidates: int


class DetectRepeat:
    def __init__(
        self,
        twenty_port: _LocationPort,
        ai_port: _RepeatAIPort | None,
        window: timedelta = timedelta(days=WINDOW_DAYS),
    ) -> None:
        self._twenty = twenty_port
        self._ai = ai_port
        self._window = window

    async def execute(
        self, *, location_id: str | None, new_dialogue: str,
    ) -> RepeatResult:
        if not location_id:
            return RepeatResult(False, None, "none", 0)

        since = datetime.now(UTC) - self._window
        recent = await self._twenty.find_recent_tasks_by_location_id(location_id, since, limit=10)
        if not recent:
            return RepeatResult(False, None, "none", 0)

        # Keyword fast path — take the most recent prior task as parent
        if _KEYWORD_RE.search(new_dialogue or ""):
            return RepeatResult(
                is_repeat=True,
                parent_task_id=str(recent[0].get("id") or "") or None,
                match_reason="keyword",
                recent_candidates=len(recent),
            )

        if self._ai is None:
            return RepeatResult(False, None, "none", len(recent))

        # Semantic path — ask the AI to match against recent candidates
        candidates = [
            {
                "id": str(r.get("id") or ""),
                "title": str(r.get("title") or ""),
                "description": _body_markdown(r.get("bodyV2")) or "",
            }
            for r in recent
        ]
        try:
            ai_out = await self._ai.check_repeat_status(new_dialogue or "", candidates)
        except Exception:
            logger.exception("check_repeat_status call failed")
            return RepeatResult(False, None, "none", len(recent))

        matches = list(ai_out.get("matches") or [])
        if not matches:
            return RepeatResult(False, None, "none", len(recent))

        matched_ids = {str(m) for m in matches}
        parent_id = next(
            (str(r["id"]) for r in recent if str(r.get("id") or "") in matched_ids),
            str(recent[0].get("id") or "") or None,
        )
        return RepeatResult(True, parent_id, "semantic", len(recent))


def _body_markdown(body: Any) -> str | None:
    """Twenty bodyV2 is {blocknote, markdown}; pull plain text safely."""
    if isinstance(body, dict):
        md = body.get("markdown")
        if isinstance(md, str):
            return md
    return None
