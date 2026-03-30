"""Тесты для CreateTwentyTaskFromSession use case."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from telegram_ingestion.domain.models import AIResult, DraftSession
from twenty_integration.application.use_cases import CreateTwentyTaskFromSession
from twenty_integration.domain.models import TwentyPerson, TwentyTask
from twenty_integration.domain.ports import TwentyCRMPort


def make_preview_session(
    user_id: int = 123,
    title: str = "Создать интеграцию",
    description: str = "Нужна интеграция с Twenty CRM",
    deadline: str | None = None,
) -> DraftSession:
    """Создать DraftSession в статусе PREVIEW."""
    ai_result = AIResult(
        title=title,
        description=description,
        category="feature",
        priority="high",
        deadline=deadline,
    )
    session = DraftSession(user_id=user_id, ai_result=ai_result)
    return session


@pytest.mark.asyncio
async def test_create_twenty_task_from_session_creates_task_and_person() -> None:
    """Должен создать новую Person и Task если Person не существует."""
    session = make_preview_session()
    assert session.ai_result is not None
    telegram_id = 123456789
    user_name = "Ivan Ivanov"

    expected_person = TwentyPerson(
        twenty_id="person-uuid-1",
        telegram_id=telegram_id,
        name=user_name,
    )
    expected_task = TwentyTask(
        twenty_id="task-uuid-1",
        title=session.ai_result.title,
        body=session.ai_result.description,
        status="TODO",
        due_at=None,
        assignee_id=None,
        person_id=None,
    )

    port = AsyncMock(spec=TwentyCRMPort)
    port.find_person_by_telegram_id = AsyncMock(return_value=None)
    port.create_person = AsyncMock(return_value=expected_person)
    port.create_task = AsyncMock(return_value=expected_task)
    port.link_person_to_task = AsyncMock()

    use_case = CreateTwentyTaskFromSession(port=port)
    result = await use_case.execute(session, telegram_id, user_name)

    assert result.twenty_id == "task-uuid-1"
    assert result.title == "Создать интеграцию"

    # Убедимся, что поиск person произошёл
    port.find_person_by_telegram_id.assert_called_once_with(telegram_id)

    # Убедимся, что создана новая person
    port.create_person.assert_called_once_with(telegram_id, user_name)

    # Убедимся, что создана task с правильными параметрами
    port.create_task.assert_called_once()
    call_kwargs = port.create_task.call_args[1]
    assert call_kwargs["title"] == "Создать интеграцию"
    assert call_kwargs["body"] == "Нужна интеграция с Twenty CRM"
    assert call_kwargs["due_at"] is None
    assert call_kwargs["assignee_id"] is None

    # Убедимся, что person связана с task
    port.link_person_to_task.assert_called_once_with("task-uuid-1", "person-uuid-1")


@pytest.mark.asyncio
async def test_create_twenty_task_finds_existing_person_by_telegram_id() -> None:
    """Должен найти существующую Person и не создавать новую."""
    session = make_preview_session()
    assert session.ai_result is not None
    telegram_id = 123456789
    user_name = "Ivan Ivanov"

    existing_person = TwentyPerson(
        twenty_id="person-existing-uuid",
        telegram_id=telegram_id,
        name="Existing Name",
    )
    expected_task = TwentyTask(
        twenty_id="task-uuid-2",
        title=session.ai_result.title,
        body=session.ai_result.description,
        status="TODO",
        due_at=None,
        assignee_id=None,
        person_id=None,
    )

    port = AsyncMock(spec=TwentyCRMPort)
    port.find_person_by_telegram_id = AsyncMock(return_value=existing_person)
    port.create_person = AsyncMock()
    port.create_task = AsyncMock(return_value=expected_task)
    port.link_person_to_task = AsyncMock()

    use_case = CreateTwentyTaskFromSession(port=port)
    result = await use_case.execute(session, telegram_id, user_name)

    assert result.twenty_id == "task-uuid-2"

    # Убедимся, что поиск person произошёл
    port.find_person_by_telegram_id.assert_called_once_with(telegram_id)

    # Убедимся, что создание новой person НЕ произошло
    port.create_person.assert_not_called()

    # Убедимся, что person связана с task (существующая)
    port.link_person_to_task.assert_called_once_with("task-uuid-2", "person-existing-uuid")


@pytest.mark.asyncio
async def test_create_twenty_task_sets_assignee_id() -> None:
    """Должен передать assignee_id в create_task."""
    session = make_preview_session()
    assert session.ai_result is not None
    telegram_id = 123456789
    user_name = "Ivan Ivanov"
    assignee_id = "member-uuid-123"

    person = TwentyPerson(
        twenty_id="person-uuid-3",
        telegram_id=telegram_id,
        name=user_name,
    )
    task = TwentyTask(
        twenty_id="task-uuid-3",
        title=session.ai_result.title,
        body=session.ai_result.description,
        status="TODO",
        due_at=None,
        assignee_id=assignee_id,
        person_id=None,
    )

    port = AsyncMock(spec=TwentyCRMPort)
    port.find_person_by_telegram_id = AsyncMock(return_value=person)
    port.create_task = AsyncMock(return_value=task)
    port.link_person_to_task = AsyncMock()

    use_case = CreateTwentyTaskFromSession(port=port)
    await use_case.execute(session, telegram_id, user_name, assignee_id=assignee_id)

    # Убедимся, что assignee_id передан в create_task
    call_kwargs = port.create_task.call_args[1]
    assert call_kwargs["assignee_id"] == assignee_id


@pytest.mark.asyncio
async def test_create_twenty_task_parses_deadline() -> None:
    """Должен парсить deadline в datetime."""
    deadline_str = "2026-04-15T10:30:00"
    session = make_preview_session(deadline=deadline_str)
    assert session.ai_result is not None
    telegram_id = 123456789
    user_name = "Ivan Ivanov"

    person = TwentyPerson(
        twenty_id="person-uuid-4",
        telegram_id=telegram_id,
        name=user_name,
    )
    task = TwentyTask(
        twenty_id="task-uuid-4",
        title=session.ai_result.title,
        body=session.ai_result.description,
        status="TODO",
        due_at=datetime.fromisoformat(deadline_str),
        assignee_id=None,
        person_id=None,
    )

    port = AsyncMock(spec=TwentyCRMPort)
    port.find_person_by_telegram_id = AsyncMock(return_value=person)
    port.create_task = AsyncMock(return_value=task)
    port.link_person_to_task = AsyncMock()

    use_case = CreateTwentyTaskFromSession(port=port)
    await use_case.execute(session, telegram_id, user_name)

    # Убедимся, что deadline был распарсен в datetime
    call_kwargs = port.create_task.call_args[1]
    assert call_kwargs["due_at"] == datetime.fromisoformat(deadline_str)


@pytest.mark.asyncio
async def test_create_twenty_task_raises_on_no_ai_result() -> None:
    """Должен выбросить ValueError если в сессии нет ai_result."""
    session = DraftSession(user_id=123)
    # ai_result не задан (None)

    port = AsyncMock(spec=TwentyCRMPort)
    use_case = CreateTwentyTaskFromSession(port=port)

    with pytest.raises(ValueError, match="должна иметь ai_result"):
        await use_case.execute(session, 123456789, "Ivan Ivanov")
