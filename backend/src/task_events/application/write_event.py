"""WriteTaskEvent — local append-only audit log.

Historically this also mirrored into a custom Twenty `taskLog` object,
but Twenty already records every CRUD change in its built-in
`timelineActivity` (with a structured diff). The mirror was redundant
and has been removed — reports now read from timelineActivity directly.

What's left: a single local repo.add() so internal flows (DetectRepeat,
future checkpoints) have a queryable history without round-tripping
through Twenty REST.
"""
from __future__ import annotations

from typing import Any

from ..domain.models import Action, ActorType, Source, TaskEvent
from ..domain.ports import TaskEventRepository


class WriteTaskEvent:
    def __init__(self, repo: TaskEventRepository) -> None:
        self._repo = repo

    async def execute(
        self,
        *,
        twenty_task_id: str,
        action: Action,
        actor_type: ActorType,  # noqa: ARG002 — kept for callers that may log actor
        user_id: int | None = None,
        actor_name: str | None = None,  # noqa: ARG002
        location_phone: str | None = None,
        priority: str | None = None,
        problem_signature: str | None = None,
        parent_task_id: str | None = None,
        script_violations: int | None = None,
        script_missing: list[str] | None = None,
        source: Source | None = None,
        meta: dict[str, Any] | None = None,
        details: str | None = None,  # noqa: ARG002
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
        await self._repo.add(event)
        return event
