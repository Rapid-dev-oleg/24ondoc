"""WriteTaskEvent — dual write to local task_events and Twenty TaskLog.

Invariant: local write MUST succeed (raises if it doesn't); the Twenty
write is best-effort and never blocks the caller.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from ..domain.models import Action, ActorType, Source, TaskEvent
from ..domain.ports import TaskEventRepository

logger = logging.getLogger(__name__)


class TaskLogMirror(Protocol):
    """Minimal interface we need from the Twenty adapter for TaskLog writes."""

    async def create_task_log(
        self,
        task_id: str,
        action: str,
        actor_type: str,
        *,
        actor_id: str | None = None,
        actor_name: str | None = None,
        details: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class WriteTaskEvent:
    def __init__(
        self,
        repo: TaskEventRepository,
        twenty_mirror: TaskLogMirror | None = None,
    ) -> None:
        self._repo = repo
        self._mirror = twenty_mirror

    async def execute(
        self,
        *,
        twenty_task_id: str,
        action: Action,
        actor_type: ActorType,
        user_id: int | None = None,
        actor_name: str | None = None,
        location_phone: str | None = None,
        priority: str | None = None,
        problem_signature: str | None = None,
        parent_task_id: str | None = None,
        script_violations: int | None = None,
        script_missing: list[str] | None = None,
        source: Source | None = None,
        meta: dict[str, Any] | None = None,
        details: str | None = None,
    ) -> TaskEvent:
        event = TaskEvent(
            twenty_task_id=twenty_task_id,
            user_id=user_id,
            location_phone=location_phone,
            action=action,
            priority=priority,
            problem_signature=problem_signature,
            parent_task_id=parent_task_id,
            script_violations=script_violations,
            script_missing=script_missing,
            source=source,
            meta=meta,
        )

        # Primary: local append-only log. If this fails, caller sees the error.
        await self._repo.add(event)

        # Best-effort Twenty mirror. Never blocks the caller on failure.
        if self._mirror is not None:
            try:
                await self._mirror.create_task_log(
                    task_id=twenty_task_id,
                    action=action.value,
                    actor_type=actor_type.value,
                    actor_id=str(user_id) if user_id is not None else None,
                    actor_name=actor_name,
                    details=details,
                    meta=meta,
                )
            except Exception:
                logger.warning(
                    "Twenty TaskLog mirror failed for task_id=%s action=%s; "
                    "local record still written",
                    twenty_task_id,
                    action.value,
                    exc_info=True,
                )

        return event
