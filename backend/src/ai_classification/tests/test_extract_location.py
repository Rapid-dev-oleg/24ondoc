"""extract_location_name — verifies enum-constrained AI resolution.

OpenRouter HTTP client is mocked; we verify:
- AI gets the catalog as a bullet list of valid display names.
- Returned name MUST be in the catalog — hallucinations are dropped.
- Whisper-distortion guidance is in the prompt.
- Fallback model is used when primary fails; full failure → None.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.ai_classification.infrastructure.openrouter_adapter import OpenRouterAdapter


def _fake_response(content: str) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value={"choices": [{"message": {"content": content}}]})
    return r


class _FakeAsyncClient:
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


CATALOG = ["Аполо 02", "Аполо 32", "Аспет 25"]


@pytest.mark.asyncio
async def test_returns_name_from_catalog() -> None:
    fake = _FakeAsyncClient([_fake_response(json.dumps({"location_name": "Аполо 32"}))])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.extract_location_name(
            "Я с аполо 32, не работает касса", CATALOG
        )
    assert result == "Аполо 32"

    sent = fake.calls[0]["json"]
    sys_msg = next(m for m in sent["messages"] if m["role"] == "system")
    # Whisper-distortion guidance must stay in the prompt
    assert "Whisper" in sys_msg["content"] or "Поло" in sys_msg["content"]
    # Catalog rendered as bullets
    for n in CATALOG:
        assert f"- {n}" in sys_msg["content"]


@pytest.mark.asyncio
async def test_hallucinated_name_dropped() -> None:
    fake = _FakeAsyncClient([_fake_response(json.dumps({"location_name": "Аполо 999"}))])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.extract_location_name("...", CATALOG)
    assert result is None


@pytest.mark.asyncio
async def test_null_in_response_yields_none() -> None:
    fake = _FakeAsyncClient([_fake_response(json.dumps({"location_name": None}))])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.extract_location_name("без точки", CATALOG)
    assert result is None


@pytest.mark.asyncio
async def test_empty_catalog_short_circuits() -> None:
    fake = _FakeAsyncClient([])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.extract_location_name("any", [])
    assert result is None
    assert not fake.calls  # no API call made


@pytest.mark.asyncio
async def test_falls_back_on_primary_error() -> None:
    fake = _FakeAsyncClient([
        httpx.HTTPError("primary down"),
        _fake_response(json.dumps({"location_name": "Аспет 25"})),
    ])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.extract_location_name("аспет 25", CATALOG)
    assert result == "Аспет 25"
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_returns_none_when_all_models_fail() -> None:
    fake = _FakeAsyncClient([
        httpx.HTTPError("primary down"),
        httpx.HTTPError("fallback down"),
    ])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.extract_location_name("any", CATALOG)
    assert result is None


@pytest.mark.asyncio
async def test_strips_markdown_code_fence() -> None:
    fake = _FakeAsyncClient([
        _fake_response("```json\n" + json.dumps({"location_name": "Аполо 02"}) + "\n```")
    ])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.extract_location_name("аполо 02", CATALOG)
    assert result == "Аполо 02"
