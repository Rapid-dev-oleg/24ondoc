"""Stage 7 — check_script: 5 mandatory phrases semantic detection."""
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


class _FakeClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def __aenter__(self): return self
    async def __aexit__(self, *_): return None

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "json": json})
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.mark.asyncio
async def test_empty_dialogue_returns_all_missing() -> None:
    adapter = OpenRouterAdapter(api_key="k")
    result = await adapter.check_script("")
    assert set(result["missing"]) == {"greeting", "ask_time", "fixed",
                                       "any_more_questions", "farewell"}
    assert result["violations_count"] == 5


@pytest.mark.asyncio
async def test_all_phrases_present_returns_zero_violations() -> None:
    fake = _FakeClient([_fake_response(json.dumps({"missing": [], "violations_count": 0}))])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.check_script("[Оператор]: Здравствуйте... [Оператор]: До свидания")
    assert result == {"missing": [], "violations_count": 0}


@pytest.mark.asyncio
async def test_some_phrases_missing() -> None:
    fake = _FakeClient([
        _fake_response(json.dumps({"missing": ["any_more_questions", "farewell"],
                                   "violations_count": 2}))
    ])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.check_script("[Оператор]: Здравствуйте. Ошибка исправлена.")
    assert result["violations_count"] == 2
    assert set(result["missing"]) == {"any_more_questions", "farewell"}


@pytest.mark.asyncio
async def test_unknown_phrase_ids_are_filtered_out() -> None:
    fake = _FakeClient([
        _fake_response(json.dumps({"missing": ["greeting", "hallucinated_phrase"],
                                   "violations_count": 2}))
    ])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.check_script("...")
    assert result["missing"] == ["greeting"]


@pytest.mark.asyncio
async def test_prompt_enumerates_all_five_ids() -> None:
    fake = _FakeClient([_fake_response(json.dumps({"missing": [], "violations_count": 0}))])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        await adapter.check_script("non-empty")
    system = next(m for m in fake.calls[0]["json"]["messages"] if m["role"] == "system")
    for pid in ("greeting", "ask_time", "fixed", "any_more_questions", "farewell"):
        assert pid in system["content"]


@pytest.mark.asyncio
async def test_fallback_on_primary_failure() -> None:
    fake = _FakeClient([
        httpx.HTTPError("primary down"),
        _fake_response(json.dumps({"missing": ["fixed"], "violations_count": 1})),
    ])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.check_script("[Оператор]: Здравствуйте")
    assert result["missing"] == ["fixed"]
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_all_models_fail_returns_safe_default() -> None:
    fake = _FakeClient([
        httpx.HTTPError("a"),
        httpx.HTTPError("b"),
    ])
    adapter = OpenRouterAdapter(api_key="k")
    with patch("httpx.AsyncClient", return_value=fake):
        result = await adapter.check_script("...")
    # Safe default: no violations counted (don't poison metrics when the
    # AI is down across the board)
    assert result == {"missing": [], "violations_count": 0}
