"""Stage 3 — Person/Location resolution in CreateTwentyTaskFromSession.

Focuses on the _resolve_person_and_location orchestration: without a phone
it's a no-op; with a phone it finds or creates Person and Location,
runs extract_location when fields are missing, and only fills empties.
"""
from __future__ import annotations

from datetime import datetime
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
        description="Аполло 32, Ленина 29. Касса не пробивает чек.",
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
    port.find_location_by_phone = AsyncMock(return_value=None)
    port.create_location = AsyncMock(return_value={"id": "loc-1"})
    port.update_location = AsyncMock()
    port.update_person_location_fields = AsyncMock()
    port.link_person_to_location = AsyncMock()
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


def _mock_ai_port(prefix: str | None = "Апполо", number: str | None = "32",
                  address: str | None = "Ленина 29") -> Any:
    ai = MagicMock()
    ai.extract_location = AsyncMock(
        return_value={"prefix": prefix, "number": number, "address": address}
    )
    return ai


@pytest.mark.asyncio
async def test_no_phone_skips_person_and_location_resolution() -> None:
    port = _mock_port()
    uc = CreateTwentyTaskFromSession(port=port, ai_port=None)

    await uc.execute(
        session=_draft(),
        telegram_id=42,
        user_name="Иван",
    )

    port.find_person_by_phone.assert_not_called()
    port.find_location_by_phone.assert_not_called()
    port.create_person_with_phone.assert_not_called()
    port.create_location.assert_not_called()
    # Task still created, without klient/location
    assert port.create_task.called
    kwargs = port.create_task.call_args.kwargs
    assert kwargs.get("klient_id") is None
    assert kwargs.get("location_rel_id") is None


@pytest.mark.asyncio
async def test_unknown_phone_creates_person_and_location_from_ai() -> None:
    port = _mock_port()
    ai = _mock_ai_port()
    uc = CreateTwentyTaskFromSession(port=port, ai_port=ai)

    await uc.execute(
        session=_draft(),
        telegram_id=42,
        user_name="Иван",
        caller_phone="79063567906",
        dialogue_text="[Оператор]: Алло. [Клиент]: Аполло 32...",
    )

    port.find_person_by_phone.assert_awaited_once_with("79063567906")
    port.create_person_with_phone.assert_awaited_once_with("79063567906")
    port.find_location_by_phone.assert_awaited_once()
    ai.extract_location.assert_awaited_once()
    port.create_location.assert_awaited_once_with(
        "79063567906", prefix="Апполо", number="32", address="Ленина 29"
    )
    port.link_person_to_location.assert_awaited_once_with("person-1", "loc-1")

    kwargs = port.create_task.call_args.kwargs
    assert kwargs["klient_id"] == "person-1"
    assert kwargs["location_rel_id"] == "loc-1"


@pytest.mark.asyncio
async def test_known_location_with_all_fields_skips_update() -> None:
    port = _mock_port()
    port.find_person_by_phone.return_value = {"id": "p", "locationPrefix": "Апполо",
                                              "locationNumber": "32", "locationAddress": "Ленина 29"}
    port.find_location_by_phone.return_value = {
        "id": "loc-existing",
        "prefix": "Апполо",
        "number": "32",
        "locationAddress": "Ленина 29",
    }
    ai = _mock_ai_port()
    uc = CreateTwentyTaskFromSession(port=port, ai_port=ai)

    await uc.execute(
        session=_draft(),
        telegram_id=42,
        user_name="Иван",
        caller_phone="79063567906",
        dialogue_text="...",
    )

    port.create_location.assert_not_called()
    port.update_location.assert_not_called()
    port.update_person_location_fields.assert_not_called()
    kwargs = port.create_task.call_args.kwargs
    assert kwargs["klient_id"] == "p"
    assert kwargs["location_rel_id"] == "loc-existing"


@pytest.mark.asyncio
async def test_known_location_with_empty_fields_fills_missing_only() -> None:
    port = _mock_port()
    port.find_person_by_phone.return_value = {"id": "p"}
    port.find_location_by_phone.return_value = {
        "id": "loc-existing",
        "prefix": "Апполо",   # already set
        "number": None,       # empty
        "locationAddress": None,  # empty
    }
    ai = _mock_ai_port(prefix="Апполо", number="32", address="Ленина 29")
    uc = CreateTwentyTaskFromSession(port=port, ai_port=ai)

    await uc.execute(
        session=_draft(),
        telegram_id=42,
        user_name="Иван",
        caller_phone="79063567906",
        dialogue_text="...",
    )

    # Only the empty fields should be patched; prefix preserved
    port.update_location.assert_awaited_once()
    patch_kwargs = port.update_location.call_args.kwargs
    assert "prefix" not in patch_kwargs
    assert patch_kwargs.get("number") == "32"
    assert patch_kwargs.get("address") == "Ленина 29"


@pytest.mark.asyncio
async def test_ai_returns_all_none_no_writes() -> None:
    port = _mock_port()
    port.find_person_by_phone.return_value = {"id": "p"}
    port.find_location_by_phone.return_value = {
        "id": "loc-existing",
        "prefix": None,
        "number": None,
        "locationAddress": None,
    }
    ai = _mock_ai_port(prefix=None, number=None, address=None)
    uc = CreateTwentyTaskFromSession(port=port, ai_port=ai)

    await uc.execute(
        session=_draft(),
        telegram_id=42,
        user_name="Иван",
        caller_phone="79063567906",
        dialogue_text="пустой диалог",
    )

    port.update_location.assert_not_called()
    port.update_person_location_fields.assert_not_called()
