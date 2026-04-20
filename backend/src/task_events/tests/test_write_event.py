"""WriteTaskEvent — local append-only log."""
from __future__ import annotations

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
async def test_writes_to_local_repo() -> None:
    repo = _FakeRepo()
    uc = WriteTaskEvent(repo=repo)

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
    assert event.twenty_task_id == "t-1"


@pytest.mark.asyncio
async def test_local_repo_failure_propagates() -> None:
    class ExplodingRepo(_FakeRepo):
        async def add(self, event: TaskEvent) -> None:
            raise RuntimeError("db down")

    uc = WriteTaskEvent(repo=ExplodingRepo())
    with pytest.raises(RuntimeError):
        await uc.execute(
            twenty_task_id="t-3",
            action=Action.ASSIGNED,
            actor_type=ActorType.ADMIN,
        )


@pytest.mark.asyncio
async def test_script_violations_fields_preserved() -> None:
    repo = _FakeRepo()
    uc = WriteTaskEvent(repo=repo)

    await uc.execute(
        twenty_task_id="t-4",
        action=Action.SCRIPT_CHECKED,
        actor_type=ActorType.SYSTEM_AI,
        script_violations=2,
        script_missing=["greeting", "farewell"],
    )

    e = repo.events[0]
    assert e.script_violations == 2
    assert e.script_missing == ["greeting", "farewell"]
