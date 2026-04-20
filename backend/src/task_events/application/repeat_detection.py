"""Repeat detection — same phone + 3-day window + (keyword OR AI semantic match).

Per the plan: a new task from the same location counts as a repeat if the
new transcript contains a trigger phrase, OR if the AI judges it to be the
same problem as one of the recent tasks.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from ..domain.models import Action
from ..domain.ports import TaskEventRepository

logger = logging.getLogger(__name__)


WINDOW_DAYS = 3

# Trigger phrases — compile once, match whole words case-insensitively
_KEYWORDS = [
    "повторное обращение",
    "опять",
    "снова",
    "та же проблема",
    "то же самое",
    "ещё раз",
    "еще раз",
    "не работает до сих пор",
    "всё ещё",
    "все еще",
    "не починили",
    "не исправили",
]
_KEYWORD_RE = re.compile(
    r"(?<![\wа-яё])(?:" + "|".join(re.escape(k) for k in _KEYWORDS) + r")(?![\wа-яё])",
    re.IGNORECASE,
)


class RepeatAICheck(Protocol):
    async def check_repeat_status(
        self,
        new_text: str,
        recent_tasks: list[dict[str, str]],
    ) -> dict[str, object]: ...


@dataclass
class RepeatResult:
    is_repeat: bool
    parent_task_id: str | None
    match_reason: str  # "keyword" | "semantic" | "none"
    recent_candidates: int


def _keyword_hit(text: str) -> bool:
    return bool(_KEYWORD_RE.search(text))


class DetectRepeat:
    """Compose a keyword fast-path with an AI semantic check."""

    def __init__(
        self,
        repo: TaskEventRepository,
        ai: RepeatAICheck | None,
        window: timedelta = timedelta(days=WINDOW_DAYS),
    ) -> None:
        self._repo = repo
        self._ai = ai
        self._window = window

    async def execute(
        self,
        *,
        location_phone: str | None,
        new_dialogue: str,
    ) -> RepeatResult:
        if not location_phone:
            return RepeatResult(False, None, "none", 0)

        since = datetime.now(UTC) - self._window
        recent = await self._repo.find_recent_by_location(
            location_phone, since=since, action=Action.CREATED, limit=10,
        )
        if not recent:
            return RepeatResult(False, None, "none", 0)

        # Keyword fast path — trust the caller but attribute to the latest prior task
        if _keyword_hit(new_dialogue):
            parent = recent[0]  # repo sorts DESC by created_at
            return RepeatResult(
                is_repeat=True,
                parent_task_id=parent.twenty_task_id,
                match_reason="keyword",
                recent_candidates=len(recent),
            )

        if self._ai is None:
            return RepeatResult(False, None, "none", len(recent))

        candidates = [
            {
                "id": e.twenty_task_id,
                "title": (e.meta or {}).get("title", ""),
                "description": (e.meta or {}).get("description", ""),
            }
            for e in recent
        ]
        try:
            ai_out = await self._ai.check_repeat_status(new_dialogue, candidates)
        except Exception:
            logger.exception("check_repeat_status call failed")
            return RepeatResult(False, None, "none", len(recent))

        matches = list(ai_out.get("matches") or [])
        if not matches:
            return RepeatResult(False, None, "none", len(recent))

        matched_ids = set(matches)
        # Pick the most recent event whose twenty_task_id is in matches
        parent = next((e for e in recent if e.twenty_task_id in matched_ids), recent[0])
        return RepeatResult(
            is_repeat=True,
            parent_task_id=parent.twenty_task_id,
            match_reason="semantic",
            recent_candidates=len(recent),
        )
