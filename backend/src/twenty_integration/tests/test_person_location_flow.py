"""Location resolution in CreateTwentyTaskFromSession.

Resolution order:
  1) cheap fold-match against catalog displayName (no IO besides the
     catalog list);
  2) AI extract_location_name as fallback;
  3) phone fallback (find_locations_by_phone) when both miss.

After a successful name- or AI-based resolve the caller's phone is fed
back into Location.additionalPhones (learn-by-resolve). Phone-based
resolves DO NOT learn — the phone is already known to belong.

Person-as-bucket-for-callers is gone. The use case never creates Person
records and never sends klient_id/personRel from the caller's phone.
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
    port.find_locations_by_phone = AsyncMock(return_value=[])
    port.find_location_by_display_name = AsyncMock(return_value=None)
    port.list_location_display_names = AsyncMock(
        return_value=["Аполо 32", "Аспет 25"]
    )
    port.add_phone_to_location = AsyncMock(return_value=False)
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
    ai.select_task_fields = AsyncMock(
        return_value=MagicMock(kategoriya=None, vazhnost=None)
    )
    ai.check_repeat_status = AsyncMock(return_value=None)
    return ai


@pytest.mark.asyncio
async def test_no_dialogue_no_phone_skips_resolution() -> None:
    port = _mock_port()
    uc = CreateTwentyTaskFromSession(port=port, ai_port=None)

    await uc.execute(session=_draft(), telegram_id=42, user_name="Иван")

    port.find_locations_by_phone.assert_not_called()
    port.list_location_display_names.assert_not_called()
    port.add_phone_to_location.assert_not_called()
    kwargs = port.create_task.call_args.kwargs
    assert kwargs.get("location_rel_id") is None


@pytest.mark.asyncio
async def test_cheap_fold_match_resolves_without_ai() -> None:
    """When the catalog name appears verbatim in the transcript, the
    cheap fold-match resolves and the AI is NEVER called."""
    port = _mock_port()
    port.find_location_by_display_name.return_value = {
        "id": "loc-32", "displayName": "Аполо 32",
    }
    ai = _mock_ai("should-not-be-called")
    uc = CreateTwentyTaskFromSession(port=port, ai_port=ai)

    await uc.execute(
        session=_draft(),
        telegram_id=42,
        user_name="Иван",
        caller_phone="79063567906",
        dialogue_text="Здравствуйте, это Аполо 32, касса не работает.",
    )

    ai.extract_location_name.assert_not_called()
    port.find_location_by_display_name.assert_awaited_once_with("Аполо 32")
    port.find_locations_by_phone.assert_not_called()
    port.add_phone_to_location.assert_awaited_once_with(
        "loc-32", "79063567906"
    )
    kwargs = port.create_task.call_args.kwargs
    assert kwargs["location_rel_id"] == "loc-32"


@pytest.mark.asyncio
async def test_cheap_match_handles_zero_strip() -> None:
    """Catalog has 'Аполо 06'; speaker said 'Аполо 6' — fold strips the
    leading zero so cheap-match still wins."""
    port = _mock_port()
    port.list_location_display_names.return_value = ["Аполо 06", "Аспет 25"]
    port.find_location_by_display_name.return_value = {
        "id": "loc-06", "displayName": "Аполо 06",
    }
    ai = _mock_ai("should-not-be-called")
    uc = CreateTwentyTaskFromSession(port=port, ai_port=ai)

    await uc.execute(
        session=_draft(),
        telegram_id=42,
        user_name="Иван",
        caller_phone="79063567906",
        dialogue_text="это аполо 6, не работает чек",
    )

    ai.extract_location_name.assert_not_called()
    port.find_location_by_display_name.assert_awaited_once_with("Аполо 06")


@pytest.mark.asyncio
async def test_ai_resolves_when_cheap_misses() -> None:
    """Whisper-mangled name ('Поло 32') doesn't survive fold-match, so we
    fall back to AI which knows the substitution."""
    port = _mock_port()
    port.find_location_by_display_name.return_value = {
        "id": "loc-32", "displayName": "Аполо 32",
    }
    ai = _mock_ai("Аполо 32")
    uc = CreateTwentyTaskFromSession(port=port, ai_port=ai)

    await uc.execute(
        session=_draft(),
        telegram_id=42,
        user_name="Иван",
        caller_phone="79063567906",
        dialogue_text="это поло 32, у нас касса встала",
    )

    ai.extract_location_name.assert_awaited_once()
    port.find_location_by_display_name.assert_awaited_with("Аполо 32")
    port.find_locations_by_phone.assert_not_called()
    port.add_phone_to_location.assert_awaited_once_with(
        "loc-32", "79063567906"
    )
    kwargs = port.create_task.call_args.kwargs
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
        dialogue_text="невнятный диалог",
    )

    port.find_locations_by_phone.assert_awaited_once_with("79063567906")
    kwargs = port.create_task.call_args.kwargs
    assert kwargs["location_rel_id"] == "loc-66"
    # Phone-based resolve does NOT learn — phone is already known
    port.add_phone_to_location.assert_not_called()


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
        dialogue_text="невнятный диалог",
    )

    kwargs = port.create_task.call_args.kwargs
    assert kwargs["location_rel_id"] is None
    port.add_phone_to_location.assert_not_called()


@pytest.mark.asyncio
async def test_use_case_never_creates_person_or_location() -> None:
    """No client-Person, no Location creation, no phantom links."""
    port = _mock_port()
    ai = _mock_ai("Аполо 32")
    uc = CreateTwentyTaskFromSession(port=port, ai_port=ai)
    await uc.execute(
        session=_draft(),
        telegram_id=42,
        user_name="Иван",
        caller_phone="79063567906",
        dialogue_text="невнятный диалог",
    )
    for missing in (
        "create_location", "update_location", "link_person_to_location",
        "update_person_location_fields", "find_person_by_phone",
        "create_person_with_phone",
    ):
        assert not hasattr(port, missing) or not getattr(port, missing).called, (
            f"{missing} must not be called by use-case"
        )
