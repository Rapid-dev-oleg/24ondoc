"""Person/Location resolution in CreateTwentyTaskFromSession.

Каталог точек теперь read-only (импортируется из xlsx). Use-case
никогда не создаёт Location и не пишет в Person кэш-поля. Резолв точки:
AI-extract имени → fallback по телефону. Ambiguous (>1) → None.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ai_classification.domain.models import (
    Category,
    ClassificationEntities,
    ClassificationResult,
    Priority,
)
from src.telegram_ingestion.domain.models import DraftSession, SessionStatus, SourceType
from src.twenty_integration.application.use_cases import CreateTwentyTaskFromSession
from src.twenty_integration.domain.models import TwentyTask
from src.twenty_integration.domain.ports import TwentyCRMPort


def _draft() -> DraftSession:
    session = DraftSession(
        user_id=42,
        status=SessionStatus.PREVIEW,
        source_type=SourceType.CALL_T2,
    )
    session.ai_result = ClassificationResult(
        source_text="test",
        title="Не работает касса",
        description="Аполо 32, Ленина 29. Касса не пробивает чек.",
        category=Category.BUG,
        priority=Priority.HIGH,
        deadline=None,
        entities=ClassificationEntities(),
        assignee_hint=None,
    )
    return session


def _mock_port() -> Any:
    port = MagicMock(spec=TwentyCRMPort)
    port.find_person_by_phone = AsyncMock(return_value=None)
    port.create_person_with_phone = AsyncMock(return_value={"id": "person-1"})
    port.find_locations_by_phone = AsyncMock(return_value=[])
    port.find_location_by_display_name = AsyncMock(return_value=None)
    port.list_location_display_names = AsyncMock(return_value=["Аполо 32", "Аспет 25"])
    port.create_task = AsyncMock(
        return_value=TwentyTask(
            twenty_id="task-1",
            title="T",
            body="B",
            status="TODO",
            due_at=None,
            assignee_id=None,
            person_id=None,
        )
    )
    port.fetch_task_field_options = AsyncMock(
        return_value={"kategoriya": [], "vazhnost": []}
    )
    return port


def _mock_ai(name: str | None = "Аполо 32") -> Any:
    ai = MagicMock()
    ai.extract_location_name = AsyncMock(return_value=name)
    return ai


@pytest.mark.asyncio
async def test_no_phone_skips_resolution() -> None:
    port = _mock_port()
    uc = CreateTwentyTaskFromSession(port=port, ai_port=None)

    await uc.execute(session=_draft(), telegram_id=42, user_name="Иван")

    port.find_person_by_phone.assert_not_called()
    port.find_locations_by_phone.assert_not_called()
    port.list_location_display_names.assert_not_called()
    kwargs = port.create_task.call_args.kwargs
    assert kwargs.get("klient_id") is None
    assert kwargs.get("location_rel_id") is None


@pytest.mark.asyncio
async def test_ai_resolves_location_from_dialogue() -> None:
    port = _mock_port()
    port.find_location_by_display_name.return_value = {"id": "loc-32", "displayName": "Аполо 32"}
    ai = _mock_ai("Аполо 32")
    uc = CreateTwentyTaskFromSession(port=port, ai_port=ai)

    await uc.execute(
        session=_draft(),
        telegram_id=42,
        user_name="Иван",
        caller_phone="79063567906",
        dialogue_text="[Клиент]: Я из аполо 32...",
    )

    port.list_location_display_names.assert_awaited_once()
    ai.extract_location_name.assert_awaited_once()
    port.find_location_by_display_name.assert_awaited_once_with("Аполо 32")
    # Phone fallback не нужен — AI уже определил.
    port.find_locations_by_phone.assert_not_called()

    kwargs = port.create_task.call_args.kwargs
    assert kwargs["klient_id"] == "person-1"
    assert kwargs["location_rel_id"] == "loc-32"


@pytest.mark.asyncio
async def test_ai_returns_none_falls_back_to_phone_unambiguous() -> None:
    port = _mock_port()
    port.find_locations_by_phone.return_value = [
        {"id": "loc-66", "displayName": "Аполо 66"}
    ]
    ai = _mock_ai(None)
    uc = CreateTwentyTaskFromSession(port=port, ai_port=ai)

    await uc.execute(
        session=_draft(),
        telegram_id=42,
        user_name="Иван",
        caller_phone="79063567906",
        dialogue_text="…",
    )

    port.find_locations_by_phone.assert_awaited_once_with("79063567906")
    kwargs = port.create_task.call_args.kwargs
    assert kwargs["location_rel_id"] == "loc-66"


@pytest.mark.asyncio
async def test_phone_ambiguous_yields_no_location() -> None:
    port = _mock_port()
    port.find_locations_by_phone.return_value = [
        {"id": "a", "displayName": "Аполо 1"},
        {"id": "b", "displayName": "Аспет 2"},
    ]
    ai = _mock_ai(None)
    uc = CreateTwentyTaskFromSession(port=port, ai_port=ai)

    await uc.execute(
        session=_draft(),
        telegram_id=42,
        user_name="Иван",
        caller_phone="79063567906",
        dialogue_text="…",
    )

    # Person still created/found, but task без точки.
    kwargs = port.create_task.call_args.kwargs
    assert kwargs["location_rel_id"] is None
    assert kwargs["klient_id"] == "person-1"


@pytest.mark.asyncio
async def test_use_case_never_creates_location() -> None:
    """Никаких create_location/update_location/link_person_to_location больше нет."""
    port = _mock_port()
    ai = _mock_ai("Аполо 32")
    uc = CreateTwentyTaskFromSession(port=port, ai_port=ai)
    await uc.execute(
        session=_draft(),
        telegram_id=42,
        user_name="Иван",
        caller_phone="79063567906",
        dialogue_text="…",
    )
    for missing in ("create_location", "update_location", "link_person_to_location",
                    "update_person_location_fields"):
        assert not hasattr(port, missing) or not getattr(port, missing).called, (
            f"{missing} must not be called by use-case"
        )
