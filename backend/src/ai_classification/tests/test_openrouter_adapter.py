"""Tests for OpenRouter adapter."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ..domain.models import Category, Priority
from ..infrastructure.openrouter_adapter import CircuitBreakerOpenError, OpenRouterAdapter

_VALID_PAYLOAD = {
    "title": "Тест",
    "description": "Описание",
    "category": "complaint",
    "priority": "high",
    "deadline": None,
    "entities": {"emails": [], "phones": [], "prices": [], "dates": []},
    "assignee_hint": None,
}


def _make_response(content: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": json.dumps(content)}}]}
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
async def test_classify_returns_classification_result() -> None:
    adapter = OpenRouterAdapter(api_key="test-key")
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.return_value = _make_response(_VALID_PAYLOAD)
        result = await adapter.classify("Жалоба на сервис")

    assert result.title == "Тест"
    assert result.category == Category.COMPLAINT
    assert result.priority == Priority.HIGH
    assert result.model_used == "anthropic/claude-3.5-sonnet"


@pytest.mark.asyncio
async def test_classify_uses_fallback_on_primary_failure() -> None:
    adapter = OpenRouterAdapter(api_key="test-key")

    async def fake_post(url: str, **kwargs: object) -> MagicMock:
        model = kwargs.get("json", {}).get("model", "")  # type: ignore[union-attr]
        if "claude" in str(model):
            raise httpx.HTTPStatusError("fail", request=MagicMock(), response=MagicMock())
        return _make_response(_VALID_PAYLOAD)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.side_effect = fake_post
        result = await adapter.classify("Test")

    assert result.model_used == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_threshold() -> None:
    adapter = OpenRouterAdapter(api_key="test-key")
    for _ in range(5):
        adapter._circuit_breaker.record_failure()

    with pytest.raises(CircuitBreakerOpenError):
        await adapter.classify("Test")


def test_circuit_breaker_resets_after_success() -> None:
    from ..infrastructure.openrouter_adapter import _CircuitBreaker

    cb = _CircuitBreaker(threshold=3)
    for _ in range(3):
        cb.record_failure()
    assert cb.is_open()
    cb.record_success()
    assert not cb.is_open()
