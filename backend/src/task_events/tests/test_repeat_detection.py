"""Stage 6 — DetectRepeat orchestration tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.task_events.application.repeat_detection import DetectRepeat
from src.task_events.domain.models import Action, Source, TaskEvent
from src.task_events.domain.ports import TaskEventRepository


class _Repo(TaskEventRepository):
    def __init__(self, recent: list[TaskEvent]) -> None:
        self._recent = recent

    async def add(self, event): return None
    async def has_action_for_task(self, *a, **kw): return False
    async def recent_by_task(self, *a, **kw): return []

    async def find_recent_by_location(
        self, location_phone, *, since, limit=10, action=Action.CREATED,
    ):
        return [e for e in self._recent if e.location_phone == location_phone and e.created_at >= since][:limit]


def _event(task_id: str, ago: timedelta, phone: str = "79000000000",
           title: str = "", description: str = "") -> TaskEvent:
    return TaskEvent(
        twenty_task_id=task_id,
        action=Action.CREATED,
        location_phone=phone,
        source=Source.CALL,
        meta={"title": title, "description": description},
        created_at=datetime.now(UTC) - ago,
    )


@pytest.mark.asyncio
async def test_no_phone_returns_not_repeat_without_db_hit() -> None:
    repo = MagicMock(spec=TaskEventRepository)
    repo.find_recent_by_location = AsyncMock()
    uc = DetectRepeat(repo=repo, ai=None)

    result = await uc.execute(location_phone=None, new_dialogue="any")

    assert result.is_repeat is False
    repo.find_recent_by_location.assert_not_called()


@pytest.mark.asyncio
async def test_no_recent_tasks_not_repeat() -> None:
    repo = _Repo([])
    uc = DetectRepeat(repo=repo, ai=MagicMock())
    result = await uc.execute(location_phone="79000000000", new_dialogue="опять проблема")
    assert result.is_repeat is False
    assert result.match_reason == "none"


@pytest.mark.asyncio
async def test_keyword_trigger_skips_ai_and_picks_latest_parent() -> None:
    recent = [
        _event("t-latest", timedelta(hours=6)),
        _event("t-older", timedelta(days=2)),
    ]
    ai = MagicMock()
    ai.check_repeat_status = AsyncMock()
    uc = DetectRepeat(repo=_Repo(recent), ai=ai)

    result = await uc.execute(
        location_phone="79000000000",
        new_dialogue="Повторное обращение, всё ещё не работает касса",
    )

    assert result.is_repeat is True
    assert result.match_reason == "keyword"
    assert result.parent_task_id == "t-latest"
    ai.check_repeat_status.assert_not_called()


@pytest.mark.asyncio
async def test_ai_match_picks_specific_parent() -> None:
    recent = [
        _event("t-latest", timedelta(hours=6), title="принтер",
               description="Не печатает чеки"),
        _event("t-older", timedelta(days=2), title="касса",
               description="Не пробивает"),
    ]
    ai = MagicMock()
    ai.check_repeat_status = AsyncMock(
        return_value={"matches": ["t-older"], "reasoning": "касса та же"}
    )
    uc = DetectRepeat(repo=_Repo(recent), ai=ai)

    result = await uc.execute(
        location_phone="79000000000",
        new_dialogue="Касса опять сбоит",  # note: 'опять' IS a keyword
    )

    # 'опять' is a keyword trigger — short-circuits before AI
    assert result.match_reason == "keyword"
    assert result.parent_task_id == "t-latest"


@pytest.mark.asyncio
async def test_ai_no_matches_not_repeat() -> None:
    recent = [_event("t-1", timedelta(hours=6))]
    ai = MagicMock()
    ai.check_repeat_status = AsyncMock(return_value={"matches": [], "reasoning": "different"})
    uc = DetectRepeat(repo=_Repo(recent), ai=ai)

    result = await uc.execute(
        location_phone="79000000000",
        new_dialogue="Новая совсем другая проблема с ЕГАИС",
    )
    assert result.is_repeat is False
    assert result.match_reason == "none"


@pytest.mark.asyncio
async def test_only_tasks_inside_window_considered() -> None:
    recent_inside = [_event("inside", timedelta(hours=12))]
    # Simulate the repo doing the filter itself — `ago > window` task won't come back
    repo = _Repo(recent_inside)
    ai = MagicMock()
    ai.check_repeat_status = AsyncMock(return_value={"matches": ["inside"], "reasoning": "yes"})
    uc = DetectRepeat(repo=repo, ai=ai)

    result = await uc.execute(
        location_phone="79000000000",
        new_dialogue="проблема с 1С",
    )
    assert result.is_repeat is True
    assert result.match_reason == "semantic"
    assert result.parent_task_id == "inside"


@pytest.mark.asyncio
async def test_ai_exception_degrades_gracefully() -> None:
    recent = [_event("t-1", timedelta(hours=6))]
    ai = MagicMock()
    ai.check_repeat_status = AsyncMock(side_effect=RuntimeError("api down"))
    uc = DetectRepeat(repo=_Repo(recent), ai=ai)

    result = await uc.execute(
        location_phone="79000000000",
        new_dialogue="какая-то проблема",
    )
    assert result.is_repeat is False
