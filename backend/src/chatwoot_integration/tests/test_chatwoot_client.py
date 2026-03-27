"""Тесты для ChatwootClient и CreateTicketFromSession."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from chatwoot_integration.domain.models import CreateTicketCommand, SupportTicket, TicketStatus
from chatwoot_integration.infrastructure.chatwoot_client import ChatwootClient

# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


def make_client(redis: AsyncMock | None = None) -> ChatwootClient:
    """Создаёт клиент с мок-Redis."""
    if redis is None:
        redis = AsyncMock()
    return ChatwootClient(
        base_url="http://chatwoot:3000",
        api_key="test-api-key",
        account_id=1,
        redis=redis,
    )


def _chatwoot_conversation_response(task_id: int = 42) -> dict[str, object]:
    return {
        "id": task_id,
        "inbox_id": 1,
        "status": "open",
        "meta": {
            "assignee": None,
        },
    }


def _chatwoot_conversations_list(task_id: int = 42) -> dict[str, object]:
    return {
        "data": {
            "meta": {"all_count": 1, "mine_count": 1, "assigned_count": 1},
            "payload": [_chatwoot_conversation_response(task_id)],
        }
    }


# ---------------------------------------------------------------------------
# create_conversation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_conversation_success() -> None:
    """create_conversation должен вернуть SupportTicket с task_id из ответа."""
    redis = AsyncMock()
    client = make_client(redis)

    command = CreateTicketCommand(
        title="Тест задача",
        description="Описание задачи",
        priority="high",
        category="bug",
        source_session_id=uuid.uuid4(),
    )

    response_data = _chatwoot_conversation_response(task_id=99)

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=response_data))
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            ticket = await client.create_conversation(command)

    assert isinstance(ticket, SupportTicket)
    assert ticket.task_id == 99
    assert ticket.status == TicketStatus.OPEN
    assert ticket.source_session_id == command.source_session_id


@pytest.mark.asyncio
async def test_create_conversation_retry_on_5xx() -> None:
    """create_conversation должен ретраить 5xx ошибки (3 попытки), затем пушить в Redis."""
    redis = AsyncMock()
    client = make_client(redis)

    command = CreateTicketCommand(
        title="Тест",
        description="Описание",
        priority="medium",
        category="other",
    )

    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503, json={"error": "Service Unavailable"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            result = await client.create_conversation(command)

    # После 3 попыток — None, сообщение в Redis
    assert result is None
    assert call_count == 3
    redis.rpush.assert_called_once()
    queue_call_args = redis.rpush.call_args
    assert "chatwoot:failed_queue" in queue_call_args[0][0]
    queued_payload = json.loads(queue_call_args[0][1])
    assert queued_payload["action"] == "create_conversation"
    assert queued_payload["command"]["title"] == "Тест"


@pytest.mark.asyncio
async def test_create_conversation_retry_on_network_error() -> None:
    """create_conversation должен ретраить сетевые ошибки."""
    redis = AsyncMock()
    client = make_client(redis)

    command = CreateTicketCommand(
        title="Тест",
        description="Описание",
        priority="low",
        category="question",
    )

    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            result = await client.create_conversation(command)

    assert result is None
    assert call_count == 3
    redis.rpush.assert_called_once()


# ---------------------------------------------------------------------------
# update_conversation_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_conversation_status_success() -> None:
    """update_conversation_status должен отправить запрос на toggle_status."""
    client = make_client()

    received_requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        received_requests.append(req)
        return httpx.Response(200, json={"current_status": "resolved"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            await client.update_conversation_status(task_id=42, status="resolved")

    assert len(received_requests) == 1
    req = received_requests[0]
    assert "/conversations/42" in str(req.url)
    body = json.loads(req.content)
    assert body["status"] == "resolved"


@pytest.mark.asyncio
async def test_update_conversation_status_retry_and_queue() -> None:
    """update_conversation_status должен ретраить и пушить в Redis при неудаче."""
    redis = AsyncMock()
    client = make_client(redis)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "Internal Server Error"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            await client.update_conversation_status(task_id=7, status="resolved")

    redis.rpush.assert_called_once()
    queued = json.loads(redis.rpush.call_args[0][1])
    assert queued["action"] == "update_conversation_status"
    assert queued["task_id"] == 7
    assert queued["status"] == "resolved"


# ---------------------------------------------------------------------------
# get_conversations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_conversations_returns_list() -> None:
    """get_conversations должен вернуть список SupportTicket."""
    client = make_client()

    response_data = _chatwoot_conversations_list(task_id=10)

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=response_data))
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            tickets = await client.get_conversations(assignee_id=5, status="open")

    assert len(tickets) == 1
    assert tickets[0].task_id == 10
    assert tickets[0].status == TicketStatus.OPEN


@pytest.mark.asyncio
async def test_get_conversations_empty_on_error() -> None:
    """get_conversations должен вернуть пустой список при ошибке API."""
    redis = AsyncMock()
    client = make_client(redis)

    transport = httpx.MockTransport(lambda req: httpx.Response(500, json={"error": "fail"}))
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            tickets = await client.get_conversations(assignee_id=5)

    assert tickets == []


# ---------------------------------------------------------------------------
# add_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_message_success() -> None:
    """add_message должен отправить POST на /messages."""
    client = make_client()

    received_requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        received_requests.append(req)
        return httpx.Response(200, json={"id": 1, "content": "hello"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            await client.add_message(task_id=42, content="hello", private=True)

    assert len(received_requests) == 1
    req = received_requests[0]
    assert "/conversations/42/messages" in str(req.url)
    body = json.loads(req.content)
    assert body["content"] == "hello"
    assert body["private"] is True


@pytest.mark.asyncio
async def test_add_message_retry_and_queue() -> None:
    """add_message должен ретраить и пушить в Redis при неудаче."""
    redis = AsyncMock()
    client = make_client(redis)

    transport = httpx.MockTransport(lambda req: httpx.Response(503, json={"error": "fail"}))
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            await client.add_message(task_id=5, content="test msg", private=False)

    redis.rpush.assert_called_once()
    queued = json.loads(redis.rpush.call_args[0][1])
    assert queued["action"] == "add_message"
    assert queued["task_id"] == 5
    assert queued["content"] == "test msg"
