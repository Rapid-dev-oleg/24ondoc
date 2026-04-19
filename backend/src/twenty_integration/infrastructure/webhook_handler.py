"""Twenty webhook endpoint — mirror admin-side task changes into task_events.

Twenty can be configured to POST events (task.created, task.updated,
task.deleted) to this endpoint when operators/admins edit a task in
the Twenty UI. We translate those into local task_events entries so
metrics don't go stale.

De-duplication: if the same twenty_task_id + action was written less
than 5s ago, we skip the webhook — chances are it's the echo of our
own write.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from task_events.application.write_event import WriteTaskEvent
from task_events.domain.models import Action, ActorType, Source

logger = logging.getLogger(__name__)

router = APIRouter()

_ECHO_WINDOW = timedelta(seconds=5)


class TwentyTaskEvent(BaseModel):
    """Permissive schema — Twenty's payload shape varies between versions."""

    eventType: str  # e.g. "task.created" | "task.updated" | "task.deleted"
    recordId: str | None = None  # Twenty task ID
    objectMetadata: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None
    record: dict[str, Any] | None = None
    updatedFields: list[str] | None = None


def _extract_task_id(payload: TwentyTaskEvent) -> str | None:
    if payload.recordId:
        return payload.recordId
    if payload.record and isinstance(payload.record.get("id"), str):
        return str(payload.record["id"])
    return None


def _map_action(event_type: str, updated_fields: list[str] | None) -> Action | None:
    if event_type.endswith(".created"):
        return Action.CREATED
    if event_type.endswith(".deleted"):
        return Action.CANCELLED
    if event_type.endswith(".updated"):
        fields = set(updated_fields or [])
        if "assigneeId" in fields or "assignee" in fields:
            return Action.ASSIGNED
        if "status" in fields or "statusZayavki" in fields:
            # Heuristic: new status VYPOLNENO / DONE / COMPLETED → completed
            return Action.STATUS_CHANGED
        return Action.STATUS_CHANGED
    return None


async def _is_recent_echo(
    write_event: WriteTaskEvent,
    twenty_task_id: str,
    action: Action,
) -> bool:
    """Return True if we wrote an identical event within the echo window."""
    try:
        recent = await write_event._repo.recent_by_task(twenty_task_id, limit=5)
    except Exception:
        return False
    cutoff = datetime.now(UTC) - _ECHO_WINDOW
    for e in recent:
        if e.action == action and e.created_at >= cutoff and e.source != Source.WEBHOOK:
            return True
    return False


@router.post("/webhook/twenty", status_code=status.HTTP_200_OK)
async def twenty_webhook(
    request: Request,
    payload: TwentyTaskEvent,
    x_twenty_secret: str | None = Header(default=None, alias="X-Twenty-Secret"),
) -> dict[str, str]:
    expected: str | None = getattr(request.state, "twenty_webhook_secret", None)
    if expected is None or x_twenty_secret != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Twenty-Secret header",
        )

    write_event: WriteTaskEvent | None = getattr(request.state, "write_task_event", None)
    if write_event is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WriteTaskEvent not available",
        )

    twenty_task_id = _extract_task_id(payload)
    if not twenty_task_id:
        logger.warning("twenty_webhook payload without task id: %s", payload.eventType)
        return {"status": "ignored"}

    action = _map_action(payload.eventType, payload.updatedFields)
    if action is None:
        logger.info("twenty_webhook ignoring eventType=%s", payload.eventType)
        return {"status": "ignored"}

    if await _is_recent_echo(write_event, twenty_task_id, action):
        logger.info(
            "twenty_webhook dedup: twenty_task_id=%s action=%s",
            twenty_task_id, action.value,
        )
        return {"status": "deduped"}

    record = payload.record or payload.properties or {}
    priority = record.get("vazhnost") or record.get("priority")

    await write_event.execute(
        twenty_task_id=twenty_task_id,
        action=action,
        actor_type=ActorType.ADMIN,
        user_id=None,
        actor_name="Twenty UI",
        priority=str(priority) if priority else None,
        source=Source.WEBHOOK,
        meta={"eventType": payload.eventType, "updatedFields": payload.updatedFields},
    )
    return {"status": "ok"}
