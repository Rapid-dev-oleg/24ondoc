"""Тесты для CreateTicketFromSession use case и webhook обработчика."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from chatwoot_integration.application.use_cases import CreateTicketFromSession
from chatwoot_integration.domain.models import CreateTicketCommand, SupportTicket, TicketStatus
from chatwoot_integration.domain.repository import ChatwootPort, SupportTicketRepository
from telegram_ingestion.domain.models import AIResult, DraftSession, SessionStatus


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


def make_preview_session(
    user_id: int = 123,
    title: str = "Баг в форме",
    description: str = "Форма падает при отправке",
    category: str = "bug",
    priority: str = "high",
) -> DraftSession:
    session = DraftSession(user_id=user_id)
    session.start_analysis()
    session.complete_analysis(
        AIResult(
            title=title,
            description=description,
            category=category,
            priority=priority,
            deadline=None,
        )
    )
    return session


# ---------------------------------------------------------------------------
# CreateTicketFromSession
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_ticket_from_session_success() -> None:
    """Должен создать тикет в Chatwoot и сохранить в репозитории."""
    session = make_preview_session()
    expected_ticket = SupportTicket(
        task_id=55,
        source_session_id=session.session_id,
        status=TicketStatus.OPEN,
        title=session.ai_result.title,  # type: ignore[union-attr]
        priority=session.ai_result.priority,  # type: ignore[union-attr]
    )

    chatwoot_port: ChatwootPort = AsyncMock(spec=ChatwootPort)
    chatwoot_port.create_conversation = AsyncMock(return_value=expected_ticket)  # type: ignore[method-assign]

    ticket_repo: SupportTicketRepository = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.save = AsyncMock()  # type: ignore[method-assign]

    use_case = CreateTicketFromSession(chatwoot_port=chatwoot_port, ticket_repo=ticket_repo)
    result = await use_case.execute(session)

    assert result is not None
    assert result.task_id == 55
    assert result.title == "Баг в форме"

    # Убедимся, что репозиторий сохранил тикет
    ticket_repo.save.assert_called_once_with(expected_ticket)

    # Убедимся, что в команде правильные данные
    call_args = chatwoot_port.create_conversation.call_args
    command: CreateTicketCommand = call_args[0][0]
    assert command.title == "Баг в форме"
    assert command.priority == "high"
    assert command.category == "bug"
    assert command.source_session_id == session.session_id


@pytest.mark.asyncio
async def test_create_ticket_from_session_no_ai_result() -> None:
    """Если у сессии нет ai_result — должен вернуть None без вызова Chatwoot."""
    session = DraftSession(user_id=999)  # статус COLLECTING, без ai_result

    chatwoot_port: ChatwootPort = AsyncMock(spec=ChatwootPort)
    ticket_repo: SupportTicketRepository = AsyncMock(spec=SupportTicketRepository)

    use_case = CreateTicketFromSession(chatwoot_port=chatwoot_port, ticket_repo=ticket_repo)
    result = await use_case.execute(session)

    assert result is None
    chatwoot_port.create_conversation.assert_not_called()
    ticket_repo.save.assert_not_called()


@pytest.mark.asyncio
async def test_create_ticket_from_session_chatwoot_failure() -> None:
    """Если Chatwoot вернул None (все ретраи исчерпаны) — результат None, в репо ничего не пишем."""
    session = make_preview_session()

    chatwoot_port: ChatwootPort = AsyncMock(spec=ChatwootPort)
    chatwoot_port.create_conversation = AsyncMock(return_value=None)  # type: ignore[method-assign]

    ticket_repo: SupportTicketRepository = AsyncMock(spec=SupportTicketRepository)

    use_case = CreateTicketFromSession(chatwoot_port=chatwoot_port, ticket_repo=ticket_repo)
    result = await use_case.execute(session)

    assert result is None
    ticket_repo.save.assert_not_called()


# ---------------------------------------------------------------------------
# Webhook: conversation_status_changed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_status_changed_updates_ticket() -> None:
    """Webhook conversation_status_changed должен обновить статус тикета в репозитории."""
    from chatwoot_integration.infrastructure.webhook_handler import process_webhook_event

    existing_ticket = SupportTicket(
        task_id=77,
        status=TicketStatus.OPEN,
        title="Тест",
    )

    ticket_repo: SupportTicketRepository = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=existing_ticket)  # type: ignore[method-assign]
    ticket_repo.save = AsyncMock()  # type: ignore[method-assign]

    payload = {
        "event": "conversation_status_changed",
        "id": 77,
        "status": "resolved",
        "meta": {},
    }

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    ticket_repo.get_by_id.assert_called_once_with(77)
    ticket_repo.save.assert_called_once()
    saved: SupportTicket = ticket_repo.save.call_args[0][0]
    assert saved.status == TicketStatus.RESOLVED


@pytest.mark.asyncio
async def test_webhook_status_changed_unknown_ticket() -> None:
    """Webhook: тикет не найден в репо — не должен упасть."""
    from chatwoot_integration.infrastructure.webhook_handler import process_webhook_event

    ticket_repo: SupportTicketRepository = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=None)  # type: ignore[method-assign]
    ticket_repo.save = AsyncMock()  # type: ignore[method-assign]

    payload = {
        "event": "conversation_status_changed",
        "id": 999,
        "status": "resolved",
        "meta": {},
    }

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    ticket_repo.save.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_unknown_event_ignored() -> None:
    """Webhook: неизвестные события игнорируются."""
    from chatwoot_integration.infrastructure.webhook_handler import process_webhook_event

    ticket_repo: SupportTicketRepository = AsyncMock(spec=SupportTicketRepository)

    payload = {"event": "message_created", "id": 1}

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    ticket_repo.get_by_id.assert_not_called()
    ticket_repo.save.assert_not_called()
