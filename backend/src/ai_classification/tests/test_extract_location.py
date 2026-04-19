"""Stage 3 — extract_location prompt + response parsing.

OpenRouter HTTP client is mocked; we verify:
- The system prompt tells the model about Whisper distortions (Поло → Апполо).
- JSON is parsed and normalised (null / "null" → None).
- Fallback model is used when primary fails.
- On total failure we return an empty dict rather than raising.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.ai_classification.infrastructure.openrouter_adapter import OpenRouterAdapter


def _fake_response(content: str) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value={"choices": [{"message": {"content": content}}]})
    return r


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient capturing requests and returning canned responses."""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict, json: dict) -> object:
        self.calls.append({"url": url, "json": json})
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.mark.asyncio
async def test_extract_location_parses_clean_json() -> None:
    fake = _FakeAsyncClient([
        _fake_response(json.dumps({"prefix": "Апполо", "number": "32", "address": "Ленина 29"}))
    ])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.extract_location("Алло, Апполо 32, Ленина 29, не работает касса")

    assert result == {"prefix": "Апполо", "number": "32", "address": "Ленина 29"}
    # System prompt should mention Whisper distortions so the model normalises them
    sent = fake.calls[0]["json"]
    system_msg = next(m for m in sent["messages"] if m["role"] == "system")
    assert "Поло" in system_msg["content"] or "Whisper" in system_msg["content"]


@pytest.mark.asyncio
async def test_extract_location_handles_nulls_and_none_strings() -> None:
    fake = _FakeAsyncClient([
        _fake_response(json.dumps({"prefix": None, "number": "null", "address": "  "}))
    ])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.extract_location("пустой звонок")
    assert result == {"prefix": None, "number": None, "address": None}


@pytest.mark.asyncio
async def test_extract_location_falls_back_on_primary_error() -> None:
    fake = _FakeAsyncClient([
        httpx.HTTPError("primary down"),
        _fake_response(json.dumps({"prefix": "Аспект", "number": "48", "address": None})),
    ])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.extract_location("Аспект 48 беспокоит")
    assert result["prefix"] == "Аспект"
    assert result["number"] == "48"
    assert result["address"] is None
    # Both models attempted
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_extract_location_returns_empty_when_all_models_fail() -> None:
    fake = _FakeAsyncClient([
        httpx.HTTPError("primary down"),
        httpx.HTTPError("fallback down"),
    ])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.extract_location("any")
    assert result == {"prefix": None, "number": None, "address": None}


@pytest.mark.asyncio
async def test_extract_location_strips_markdown_code_fence() -> None:
    fake = _FakeAsyncClient([
        _fake_response(
            "```json\n" + json.dumps({"prefix": "Апполо", "number": "6", "address": None}) + "\n```"
        )
    ])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.extract_location("Апполо 6")
    assert result == {"prefix": "Апполо", "number": "6", "address": None}
