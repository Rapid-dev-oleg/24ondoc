"""Stage 5 — WriteTaskEvent dual-write invariants."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.task_events.application.write_event import WriteTaskEvent
from src.task_events.domain.models import Action, ActorType, Source, TaskEvent
from src.task_events.domain.ports import TaskEventRepository


class _FakeRepo(TaskEventRepository):
    def __init__(self) -> None:
        self.events: list[TaskEvent] = []

    async def add(self, event: TaskEvent) -> None:
        self.events.append(event)

    async def find_recent_by_location(
        self, location_phone, *, since, limit=10, action=Action.CREATED,
    ):
        return []

    async def has_action_for_task(self, twenty_task_id, action):
        return False

    async def recent_by_task(self, twenty_task_id, limit=20):
        return list(self.events)


@pytest.mark.asyncio
async def test_writes_local_first_then_mirrors_to_twenty() -> None:
    repo = _FakeRepo()
    mirror = MagicMock()
    mirror.create_task_log = AsyncMock(return_value={"id": "log-1"})
    uc = WriteTaskEvent(repo=repo, twenty_mirror=mirror)

    event = await uc.execute(
        twenty_task_id="t-1",
        action=Action.CREATED,
        actor_type=ActorType.OPERATOR,
        user_id=42,
        location_phone="79063567906",
        source=Source.CALL,
    )

    assert len(repo.events) == 1
    assert repo.events[0].action == Action.CREATED
    assert repo.events[0].twenty_task_id == "t-1"
    mirror.create_task_log.assert_awaited_once()
    kwargs = mirror.create_task_log.call_args.kwargs
    assert kwargs["action"] == "CREATED"
    assert kwargs["actor_type"] == "OPERATOR"
    assert event.twenty_task_id == "t-1"


@pytest.mark.asyncio
async def test_twenty_mirror_failure_does_not_fail_write() -> None:
    repo = _FakeRepo()
    mirror = MagicMock()
    mirror.create_task_log = AsyncMock(side_effect=RuntimeError("twenty down"))
    uc = WriteTaskEvent(repo=repo, twenty_mirror=mirror)

    # Should NOT raise even though mirror failed
    await uc.execute(
        twenty_task_id="t-2",
        action=Action.COMPLETED,
        actor_type=ActorType.OPERATOR,
    )

    assert len(repo.events) == 1
    assert repo.events[0].action == Action.COMPLETED


@pytest.mark.asyncio
async def test_local_repo_failure_propagates_and_skips_mirror() -> None:
    class ExplodingRepo(_FakeRepo):
        async def add(self, event: TaskEvent) -> None:
            raise RuntimeError("db down")

    repo = ExplodingRepo()
    mirror = MagicMock()
    mirror.create_task_log = AsyncMock()
    uc = WriteTaskEvent(repo=repo, twenty_mirror=mirror)

    with pytest.raises(RuntimeError):
        await uc.execute(
            twenty_task_id="t-3",
            action=Action.ASSIGNED,
            actor_type=ActorType.ADMIN,
        )

    mirror.create_task_log.assert_not_called()


@pytest.mark.asyncio
async def test_works_without_mirror() -> None:
    repo = _FakeRepo()
    uc = WriteTaskEvent(repo=repo, twenty_mirror=None)

    await uc.execute(
        twenty_task_id="t-4",
        action=Action.SCRIPT_CHECKED,
        actor_type=ActorType.SYSTEM_AI,
        script_violations=2,
        script_missing=["greeting", "farewell"],
    )

    assert len(repo.events) == 1
    e = repo.events[0]
    assert e.script_violations == 2
    assert e.script_missing == ["greeting", "farewell"]
