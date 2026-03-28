"""Тесты для ChatwootPlatformClient."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from chatwoot_integration.domain.models import ChatwootAgent
from chatwoot_integration.infrastructure.platform_client import ChatwootPlatformClient

# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


def make_client(redis: AsyncMock | None = None) -> ChatwootPlatformClient:
    """Создаёт Platform клиент с мок-Redis."""
    if redis is None:
        redis = AsyncMock()
    return ChatwootPlatformClient(
        base_url="http://chatwoot:3000",
        platform_api_key="platform-test-key",
        redis=redis,
    )


def _user_response(user_id: int = 10, access_token: str = "tok-abc") -> dict[str, object]:
    return {"id": user_id, "access_token": access_token, "email": "test@example.com"}


def _account_user_response() -> dict[str, object]:
    return {"id": 1, "user_id": 10, "role": "agent"}


def _sso_response(
    url: str = "https://chatwoot.example.com/auth/sign_in?token=xyz",
) -> dict[str, object]:
    return {"url": url}


# ---------------------------------------------------------------------------
# create_user — успех
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_user_success() -> None:
    """create_user возвращает ChatwootAgent с user_id и access_token."""
    client = make_client()
    resp_data = _user_response(user_id=42, access_token="tok-42")

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=resp_data))
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            agent = await client.create_user("Тест", "test@example.com")

    assert isinstance(agent, ChatwootAgent)
    assert agent.user_id == 42
    assert agent.access_token == "tok-42"


@pytest.mark.asyncio
async def test_create_user_sends_correct_payload() -> None:
    """create_user отправляет name, email, confirmed=True."""
    import json

    client = make_client()
    received: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        received.append(req.content)
        return httpx.Response(200, json=_user_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            await client.create_user("Иван", "ivan@example.com")

    body = json.loads(received[0])
    assert body["name"] == "Иван"
    assert body["email"] == "ivan@example.com"
    assert body["confirmed"] is True


@pytest.mark.asyncio
async def test_create_user_missing_access_token_defaults_to_empty() -> None:
    """create_user возвращает пустой access_token если его нет в ответе."""
    client = make_client()
    resp_data: dict[str, object] = {"id": 5}

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=resp_data))
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            agent = await client.create_user("X", "x@example.com")

    assert agent.user_id == 5
    assert agent.access_token == ""


# ---------------------------------------------------------------------------
# create_user — retry и fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_user_retries_on_5xx() -> None:
    """create_user делает retry при 500."""
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200, json=_user_response())

    client = make_client()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            agent = await client.create_user("X", "x@example.com")

    assert call_count == 3
    assert agent.user_id == 10


@pytest.mark.asyncio
async def test_create_user_queues_on_persistent_failure() -> None:
    """create_user пишет в Redis и бросает исключение при постоянной ошибке."""
    redis = AsyncMock()
    client = make_client(redis)

    transport = httpx.MockTransport(lambda req: httpx.Response(500, text="Error"))
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            with pytest.raises(Exception, match="Platform API"):
                await client.create_user("X", "x@example.com")

    redis.rpush.assert_called_once()
    call_args = redis.rpush.call_args[0]
    assert call_args[0] == "chatwoot:platform_failed_queue"


@pytest.mark.asyncio
async def test_create_user_raises_on_4xx() -> None:
    """create_user бросает исключение и пушит в очередь при 4xx ошибке."""
    redis = AsyncMock()
    client = make_client(redis)

    transport = httpx.MockTransport(lambda req: httpx.Response(422, text="Unprocessable Entity"))
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            with pytest.raises(Exception, match="Platform API"):
                await client.create_user("X", "x@example.com")

    # При ошибке — пишется в очередь
    redis.rpush.assert_called_once()


# ---------------------------------------------------------------------------
# add_to_account — успех
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_to_account_success() -> None:
    """add_to_account выполняется без исключений при 200."""
    client = make_client()

    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=_account_user_response())
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            await client.add_to_account(user_id=10, account_id=2, role="agent")


@pytest.mark.asyncio
async def test_add_to_account_sends_correct_payload() -> None:
    """add_to_account отправляет user_id и role."""
    import json

    client = make_client()
    received: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        received.append(req.content)
        return httpx.Response(200, json=_account_user_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            await client.add_to_account(user_id=7, account_id=2, role="administrator")

    body = json.loads(received[0])
    assert body["user_id"] == 7
    assert body["role"] == "administrator"


@pytest.mark.asyncio
async def test_add_to_account_default_role_is_agent() -> None:
    """add_to_account использует role=agent по умолчанию."""
    import json

    client = make_client()
    received: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        received.append(req.content)
        return httpx.Response(200, json=_account_user_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            await client.add_to_account(user_id=5, account_id=1)

    body = json.loads(received[0])
    assert body["role"] == "agent"


@pytest.mark.asyncio
async def test_add_to_account_uses_correct_account_url() -> None:
    """add_to_account POST на /platform/api/v1/accounts/{id}/account_users."""
    client = make_client()
    captured_urls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured_urls.append(str(req.url))
        return httpx.Response(200, json=_account_user_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            await client.add_to_account(user_id=10, account_id=5)

    assert "/platform/api/v1/accounts/5/account_users" in captured_urls[0]


@pytest.mark.asyncio
async def test_add_to_account_retries_on_5xx() -> None:
    """add_to_account делает retry при 500."""
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(500, text="Server Error")
        return httpx.Response(200, json=_account_user_response())

    client = make_client()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            await client.add_to_account(user_id=10, account_id=2)

    assert call_count == 3


@pytest.mark.asyncio
async def test_add_to_account_queues_on_failure() -> None:
    """add_to_account пишет в Redis при постоянной ошибке."""
    redis = AsyncMock()
    client = make_client(redis)

    transport = httpx.MockTransport(lambda req: httpx.Response(500, text="Error"))
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            with pytest.raises(Exception, match="Platform API"):
                await client.add_to_account(user_id=10, account_id=2)

    redis.rpush.assert_called_once()


# ---------------------------------------------------------------------------
# get_sso_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sso_url_success() -> None:
    """get_sso_url возвращает URL из ответа."""
    client = make_client()
    expected = "https://chatwoot.example.com/auth/sign_in?token=sso123"
    resp_data = _sso_response(url=expected)

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=resp_data))
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            url = await client.get_sso_url(user_id=99)

    assert url == expected


@pytest.mark.asyncio
async def test_get_sso_url_uses_correct_endpoint() -> None:
    """get_sso_url GET на /platform/api/v1/users/{id}/login."""
    client = make_client()
    captured_urls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured_urls.append(str(req.url))
        return httpx.Response(200, json=_sso_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            await client.get_sso_url(user_id=77)

    assert "/platform/api/v1/users/77/login" in captured_urls[0]


@pytest.mark.asyncio
async def test_get_sso_url_retries_on_5xx() -> None:
    """get_sso_url делает retry при 500."""
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(500, text="Error")
        return httpx.Response(200, json=_sso_response())

    client = make_client()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            url = await client.get_sso_url(user_id=1)

    assert call_count == 3
    assert url != ""


@pytest.mark.asyncio
async def test_get_sso_url_raises_on_persistent_failure() -> None:
    """get_sso_url бросает исключение при постоянной 5xx ошибке."""
    redis = AsyncMock()
    client = make_client(redis)

    transport = httpx.MockTransport(lambda req: httpx.Response(500, text="Error"))
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            with pytest.raises(Exception, match="Platform API"):
                await client.get_sso_url(user_id=1)


@pytest.mark.asyncio
async def test_get_sso_url_missing_url_defaults_to_empty() -> None:
    """get_sso_url возвращает пустую строку если url отсутствует в ответе."""
    client = make_client()

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        with patch.object(client, "_http", http):
            url = await client.get_sso_url(user_id=1)

    assert url == ""


# ---------------------------------------------------------------------------
# Domain models tests
# ---------------------------------------------------------------------------


def test_chatwoot_agent_defaults() -> None:
    """ChatwootAgent создаётся с пустыми access_token и sso_url по умолчанию."""
    agent = ChatwootAgent(user_id=1)
    assert agent.user_id == 1
    assert agent.access_token == ""
    assert agent.sso_url == ""


def test_chatwoot_agent_with_all_fields() -> None:
    """ChatwootAgent хранит все поля."""
    agent = ChatwootAgent(
        user_id=42,
        access_token="tok-xyz",
        sso_url="https://example.com/login?token=abc",
    )
    assert agent.user_id == 42
    assert agent.access_token == "tok-xyz"
    assert agent.sso_url == "https://example.com/login?token=abc"


# ---------------------------------------------------------------------------
# Domain events tests
# ---------------------------------------------------------------------------


def test_agent_created_event_defaults() -> None:
    """AgentCreated создаётся с нулевыми значениями по умолчанию."""
    from chatwoot_integration.domain.events import AgentCreated

    event = AgentCreated()
    assert event.agent_id == 0
    assert event.name == ""
    assert event.email == ""
    assert event.occurred_at is not None


def test_agent_created_event_with_data() -> None:
    """AgentCreated хранит agent_id, name, email."""
    from chatwoot_integration.domain.events import AgentCreated

    event = AgentCreated(agent_id=5, name="Иван", email="ivan@test.com")
    assert event.agent_id == 5
    assert event.name == "Иван"
    assert event.email == "ivan@test.com"


def test_agent_token_obtained_defaults() -> None:
    """AgentTokenObtained создаётся с нулевыми значениями по умолчанию."""
    from chatwoot_integration.domain.events import AgentTokenObtained

    event = AgentTokenObtained()
    assert event.agent_id == 0
    assert event.sso_url == ""


def test_agent_token_obtained_with_data() -> None:
    """AgentTokenObtained хранит agent_id и sso_url."""
    from chatwoot_integration.domain.events import AgentTokenObtained

    event = AgentTokenObtained(agent_id=7, sso_url="https://cw.example.com/sign_in?t=abc")
    assert event.agent_id == 7
    assert event.sso_url == "https://cw.example.com/sign_in?t=abc"
