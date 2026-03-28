"""Tests for ChatwootRegisterAdapter — platform API и application API пути."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from telegram_ingestion.infrastructure.chatwoot_register_adapter import ChatwootRegisterAdapter


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------


def _make_adapter(platform_api_key: str | None = None) -> ChatwootRegisterAdapter:
    return ChatwootRegisterAdapter(
        base_url="http://chatwoot:3000",
        api_key="app-key",
        account_id=2,
        platform_api_key=platform_api_key,
    )


def _user_response(user_id: int = 10) -> dict[str, object]:
    return {"id": user_id, "email": "test@example.com", "name": "Test"}


def _account_user_response() -> dict[str, object]:
    return {"id": 1, "user_id": 10, "role": "agent"}


def _agent_response(agent_id: int = 5) -> dict[str, object]:
    return {"id": agent_id, "email": "test@example.com", "name": "Test", "role": "agent"}


# ---------------------------------------------------------------------------
# Тесты: Application API (без platform_api_key)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_application_api_used_when_no_platform_key() -> None:
    """Когда platform_api_key не задан, используется Application API."""
    adapter = _make_adapter(platform_api_key=None)
    captured: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(str(req.url))
        return httpx.Response(200, json=_agent_response(agent_id=7))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        adapter._http = http
        result = await adapter.create_chatwoot_agent("Иван", "ivan@24ondoc.ru", "pass123!")

    assert result == 7
    assert any("/api/v1/accounts/2/agents" in url for url in captured)


@pytest.mark.asyncio
async def test_application_api_sends_correct_payload() -> None:
    """Application API отправляет name, email, password, role=agent."""
    adapter = _make_adapter(platform_api_key=None)
    received: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        received.append(req.content)
        return httpx.Response(200, json=_agent_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        adapter._http = http
        await adapter.create_chatwoot_agent("Мария", "maria@24ondoc.ru", "Secr3t!")

    body = json.loads(received[0])
    assert body["name"] == "Мария"
    assert body["email"] == "maria@24ondoc.ru"
    assert body["password"] == "Secr3t!"
    assert body["role"] == "agent"


@pytest.mark.asyncio
async def test_application_api_raises_on_4xx() -> None:
    """Application API пробрасывает ошибку при 4xx."""
    adapter = _make_adapter(platform_api_key=None)

    transport = httpx.MockTransport(lambda req: httpx.Response(422, text="Unprocessable"))
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        adapter._http = http
        with pytest.raises(RuntimeError, match="422"):
            await adapter.create_chatwoot_agent("X", "x@24ondoc.ru", "pass!")


# ---------------------------------------------------------------------------
# Тесты: Platform API (с platform_api_key)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_api_creates_user_and_links_to_account() -> None:
    """Platform API: создаёт пользователя и добавляет в аккаунт 2."""
    adapter = _make_adapter(platform_api_key="platform-key")
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        calls.append(path)
        if path == "/platform/api/v1/users":
            return httpx.Response(200, json=_user_response(user_id=42))
        if "/account_users" in path:
            return httpx.Response(200, json=_account_user_response())
        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        adapter._platform_http = http
        result = await adapter.create_chatwoot_agent("Алиса", "alice@24ondoc.ru", "Pass1!")

    assert result == 42
    assert "/platform/api/v1/users" in calls
    assert any("/platform/api/v1/accounts/2/account_users" in c for c in calls)


@pytest.mark.asyncio
async def test_platform_api_sends_correct_user_payload() -> None:
    """Platform API отправляет name, email, password, confirmed=True при создании юзера."""
    adapter = _make_adapter(platform_api_key="platform-key")
    received: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/platform/api/v1/users":
            received.append(req.content)
            return httpx.Response(200, json=_user_response(user_id=10))
        return httpx.Response(200, json=_account_user_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        adapter._platform_http = http
        await adapter.create_chatwoot_agent("Боб", "bob@24ondoc.ru", "B0bP@ss!")

    body = json.loads(received[0])
    assert body["name"] == "Боб"
    assert body["email"] == "bob@24ondoc.ru"
    assert body["password"] == "B0bP@ss!"
    assert body["confirmed"] is True


@pytest.mark.asyncio
async def test_platform_api_sends_correct_account_users_payload() -> None:
    """Platform API отправляет user_id и role=agent при добавлении в аккаунт."""
    adapter = _make_adapter(platform_api_key="platform-key")
    account_body: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/platform/api/v1/users":
            return httpx.Response(200, json=_user_response(user_id=99))
        account_body.append(req.content)
        return httpx.Response(200, json=_account_user_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        adapter._platform_http = http
        await adapter.create_chatwoot_agent("Тест", "test@24ondoc.ru", "T3st!")

    body = json.loads(account_body[0])
    assert body["user_id"] == 99
    assert body["role"] == "agent"


@pytest.mark.asyncio
async def test_platform_api_user_creation_error_raises() -> None:
    """Ошибка создания платформ-юзера пробрасывается как RuntimeError."""
    adapter = _make_adapter(platform_api_key="platform-key")

    transport = httpx.MockTransport(lambda req: httpx.Response(422, text="Email taken"))
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        adapter._platform_http = http
        with pytest.raises(RuntimeError, match="Platform user creation failed"):
            await adapter.create_chatwoot_agent("X", "x@24ondoc.ru", "pass!")


@pytest.mark.asyncio
async def test_platform_api_account_link_error_raises() -> None:
    """Ошибка привязки к аккаунту пробрасывается как RuntimeError."""
    adapter = _make_adapter(platform_api_key="platform-key")
    deleted_ids: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/platform/api/v1/users":
            return httpx.Response(200, json=_user_response(user_id=15))
        if "/account_users" in req.url.path:
            return httpx.Response(403, text="Forbidden")
        if req.method == "DELETE":
            deleted_ids.append(int(req.url.path.split("/")[-1]))
            return httpx.Response(200, text="")
        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        adapter._platform_http = http
        with pytest.raises(RuntimeError, match="Failed to add Chatwoot user"):
            await adapter.create_chatwoot_agent("Y", "y@24ondoc.ru", "pass!")


@pytest.mark.asyncio
async def test_platform_api_rollback_on_account_link_failure() -> None:
    """При ошибке привязки к аккаунту созданный пользователь удаляется (rollback)."""
    adapter = _make_adapter(platform_api_key="platform-key")
    deleted_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/platform/api/v1/users":
            return httpx.Response(200, json=_user_response(user_id=33))
        if "/account_users" in req.url.path:
            return httpx.Response(500, text="Server Error")
        if req.method == "DELETE" and "/platform/api/v1/users/" in req.url.path:
            deleted_paths.append(req.url.path)
            return httpx.Response(200, text="")
        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        adapter._platform_http = http
        with pytest.raises(RuntimeError):
            await adapter.create_chatwoot_agent("Z", "z@24ondoc.ru", "pass!")

    assert any("/platform/api/v1/users/33" in p for p in deleted_paths), (
        f"Rollback DELETE not called; captured paths: {deleted_paths}"
    )


@pytest.mark.asyncio
async def test_platform_api_logs_user_creation(caplog: pytest.LogCaptureFixture) -> None:
    """Platform API логирует имя и email при создании пользователя (без пароля)."""
    adapter = _make_adapter(platform_api_key="platform-key")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/platform/api/v1/users":
            return httpx.Response(200, json=_user_response(user_id=20))
        return httpx.Response(200, json=_account_user_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        adapter._platform_http = http
        with caplog.at_level(logging.DEBUG, logger="telegram_ingestion.infrastructure.chatwoot_register_adapter"):
            await adapter.create_chatwoot_agent("Логгер", "logger@24ondoc.ru", "pass!")

    log_text = caplog.text
    assert "Логгер" in log_text or "logger@24ondoc.ru" in log_text
    # Пароль НЕ должен попасть в лог
    assert "pass!" not in log_text


@pytest.mark.asyncio
async def test_platform_api_logs_response_status(caplog: pytest.LogCaptureFixture) -> None:
    """Platform API логирует статус ответа при создании пользователя."""
    adapter = _make_adapter(platform_api_key="platform-key")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/platform/api/v1/users":
            return httpx.Response(200, json=_user_response(user_id=21))
        return httpx.Response(200, json=_account_user_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        adapter._platform_http = http
        with caplog.at_level(logging.DEBUG, logger="telegram_ingestion.infrastructure.chatwoot_register_adapter"):
            await adapter.create_chatwoot_agent("Статус", "status@24ondoc.ru", "pass!")

    assert "200" in caplog.text


@pytest.mark.asyncio
async def test_platform_api_logs_account_users_request(caplog: pytest.LogCaptureFixture) -> None:
    """Platform API логирует запрос добавления в аккаунт."""
    adapter = _make_adapter(platform_api_key="platform-key")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/platform/api/v1/users":
            return httpx.Response(200, json=_user_response(user_id=55))
        return httpx.Response(200, json=_account_user_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://chatwoot:3000") as http:
        adapter._platform_http = http
        with caplog.at_level(logging.DEBUG, logger="telegram_ingestion.infrastructure.chatwoot_register_adapter"):
            await adapter.create_chatwoot_agent("Аккаунт", "acc@24ondoc.ru", "pass!")

    assert "55" in caplog.text  # user_id=55 упоминается в логах account linking
    assert "2" in caplog.text   # account_id=2 упоминается в логах
