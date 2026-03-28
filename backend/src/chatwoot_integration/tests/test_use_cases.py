"""Тесты для CreateTicketFromSession use case и webhook обработчика."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from chatwoot_integration.application.use_cases import CreateTicketFromSession
from chatwoot_integration.domain.events import TicketCreated, TicketCreationFailed, TicketUpdated
from chatwoot_integration.domain.models import CreateTicketCommand, SupportTicket, TicketStatus
from chatwoot_integration.domain.repository import ChatwootPort, SupportTicketRepository
from telegram_ingestion.domain.models import AIResult, DraftSession

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
    assert session.ai_result is not None
    expected_ticket = SupportTicket(
        task_id=55,
        source_session_id=session.session_id,
        status=TicketStatus.OPEN,
        title=session.ai_result.title,
        priority=session.ai_result.priority,
    )

    chatwoot_port = AsyncMock(spec=ChatwootPort)
    chatwoot_port.create_conversation = AsyncMock(return_value=expected_ticket)

    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.save = AsyncMock()

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

    chatwoot_port = AsyncMock(spec=ChatwootPort)
    ticket_repo = AsyncMock(spec=SupportTicketRepository)

    use_case = CreateTicketFromSession(chatwoot_port=chatwoot_port, ticket_repo=ticket_repo)
    result = await use_case.execute(session)

    assert result is None
    chatwoot_port.create_conversation.assert_not_called()
    ticket_repo.save.assert_not_called()


@pytest.mark.asyncio
async def test_create_ticket_from_session_chatwoot_failure() -> None:
    """Если Chatwoot вернул None (все ретраи исчерпаны) — результат None, в репо ничего не пишем."""
    session = make_preview_session()

    chatwoot_port = AsyncMock(spec=ChatwootPort)
    chatwoot_port.create_conversation = AsyncMock(return_value=None)

    ticket_repo = AsyncMock(spec=SupportTicketRepository)

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

    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=existing_ticket)
    ticket_repo.save = AsyncMock()

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

    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=None)
    ticket_repo.save = AsyncMock()

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

    ticket_repo = AsyncMock(spec=SupportTicketRepository)

    payload = {"event": "message_created", "id": 1}

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    ticket_repo.get_by_id.assert_not_called()
    ticket_repo.save.assert_not_called()


# ---------------------------------------------------------------------------
# Domain Events
# ---------------------------------------------------------------------------


def test_ticket_created_event() -> None:
    """TicketCreated — проверяем создание и поля."""
    session_id = uuid.uuid4()
    event = TicketCreated(task_id=42, session_id=session_id, permalink="http://example.com")
    assert event.task_id == 42
    assert event.session_id == session_id
    assert event.permalink == "http://example.com"
    assert event.occurred_at is not None


def test_ticket_updated_event() -> None:
    """TicketUpdated — проверяем создание и поля."""
    event = TicketUpdated(task_id=7, new_status="resolved")
    assert event.task_id == 7
    assert event.new_status == "resolved"


def test_ticket_creation_failed_event() -> None:
    """TicketCreationFailed — проверяем создание и поля."""
    session_id = uuid.uuid4()
    event = TicketCreationFailed(session_id=session_id, reason="Chatwoot unreachable")
    assert event.session_id == session_id
    assert event.reason == "Chatwoot unreachable"


def test_domain_event_defaults() -> None:
    """DomainEvent с дефолтными значениями для всех событий."""
    e1 = TicketCreated()
    assert e1.task_id == 0
    assert e1.permalink == ""

    e2 = TicketUpdated()
    assert e2.task_id == 0
    assert e2.new_status == ""

    e3 = TicketCreationFailed()
    assert e3.reason == ""


# ---------------------------------------------------------------------------
# Webhook: граничные случаи process_webhook_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_missing_task_id_does_not_save() -> None:
    """Webhook без 'id' — не должен вызывать репозиторий."""
    from chatwoot_integration.infrastructure.webhook_handler import process_webhook_event

    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    payload = {"event": "conversation_status_changed", "status": "resolved"}

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    ticket_repo.get_by_id.assert_not_called()
    ticket_repo.save.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_unknown_status_does_not_save() -> None:
    """Webhook с неизвестным статусом — не должен вызывать save."""
    from chatwoot_integration.infrastructure.webhook_handler import process_webhook_event

    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    payload = {"event": "conversation_status_changed", "id": 10, "status": "unknown_status"}

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    ticket_repo.save.assert_not_called()


_VALID_TOKEN = "test-webhook-token"


@pytest.mark.asyncio
async def test_chatwoot_webhook_endpoint_no_repo_raises_503() -> None:
    """HTTP endpoint: repo недоступен → 503."""
    from fastapi import FastAPI, Request

    from chatwoot_integration.infrastructure.webhook_handler import router

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def inject_token(request: Request, call_next: object) -> object:
        request.state.chatwoot_webhook_token = _VALID_TOKEN
        return await call_next(request)  # type: ignore[operator]

    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/webhook/chatwoot",
        json={"event": "conversation_status_changed", "id": 1, "status": "resolved", "token": _VALID_TOKEN},
    )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_chatwoot_webhook_endpoint_with_repo_returns_ok() -> None:
    """HTTP endpoint: repo доступен → 200 ok."""
    from fastapi import FastAPI, Request

    from chatwoot_integration.infrastructure.webhook_handler import router

    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=None)

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def inject_state(request: Request, call_next: object) -> object:
        request.state.ticket_repo = ticket_repo
        request.state.chatwoot_webhook_token = _VALID_TOKEN
        _next = call_next
        return await _next(request)  # type: ignore[operator]

    client = TestClient(app)
    response = client.post(
        "/webhook/chatwoot",
        json={"event": "conversation_status_changed", "id": 99, "status": "resolved", "token": _VALID_TOKEN},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_webhook_valid_token_returns_ok() -> None:
    """HTTP endpoint: валидный токен в теле → 200 ok."""
    from fastapi import FastAPI, Request

    from chatwoot_integration.infrastructure.webhook_handler import router

    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=None)

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def inject_state(request: Request, call_next: object) -> object:
        request.state.ticket_repo = ticket_repo
        request.state.chatwoot_webhook_token = _VALID_TOKEN
        return await call_next(request)  # type: ignore[operator]

    client = TestClient(app)
    response = client.post(
        "/webhook/chatwoot",
        json={"event": "message_created", "id": 1, "token": _VALID_TOKEN},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_webhook_invalid_token_returns_401() -> None:
    """HTTP endpoint: невалидный токен → 401."""
    from fastapi import FastAPI, Request

    from chatwoot_integration.infrastructure.webhook_handler import router

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def inject_token(request: Request, call_next: object) -> object:
        request.state.chatwoot_webhook_token = _VALID_TOKEN
        return await call_next(request)  # type: ignore[operator]

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/webhook/chatwoot",
        json={"event": "conversation_status_changed", "id": 1, "status": "resolved", "token": "wrong-token"},
    )
    assert response.status_code == 401
