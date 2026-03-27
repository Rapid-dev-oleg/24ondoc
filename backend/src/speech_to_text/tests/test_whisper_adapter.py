"""Tests for Whisper adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from ..infrastructure.whisper_adapter import WhisperAdapter


@pytest.mark.asyncio
async def test_transcribe_uses_self_hosted() -> None:
    adapter = WhisperAdapter(self_hosted_url="http://whisper:9000", openai_api_key="")
    mock_resp = MagicMock()
    mock_resp.text = "Привет мир"
    mock_resp.raise_for_status = MagicMock()

    with (
        patch("httpx.AsyncClient") as mock_cls,
        patch("builtins.open", mock_open(read_data=b"audio")),
    ):
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.return_value = mock_resp
        result = await adapter.transcribe("/tmp/audio.ogg", "ru")

    assert result == "Привет мир"
    call_url: str = mock_client.post.call_args[0][0]
    assert "whisper:9000" in call_url


@pytest.mark.asyncio
async def test_transcribe_falls_back_to_openai() -> None:
    adapter = WhisperAdapter(self_hosted_url="http://whisper:9000", openai_api_key="test-key")
    call_urls: list[str] = []

    async def fake_post(url: str, **kwargs: object) -> MagicMock:
        call_urls.append(url)
        if "whisper:9000" in url:
            raise Exception("Self-hosted unavailable")
        resp = MagicMock()
        resp.json.return_value = {"text": "OpenAI transcription"}
        resp.raise_for_status = MagicMock()
        return resp

    with (
        patch("httpx.AsyncClient") as mock_cls,
        patch("builtins.open", mock_open(read_data=b"audio")),
    ):
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.side_effect = fake_post
        result = await adapter.transcribe("/tmp/audio.ogg", "ru")

    assert result == "OpenAI transcription"
    assert any("openai.com" in url for url in call_urls)


@pytest.mark.asyncio
async def test_transcribe_raises_when_both_fail() -> None:
    adapter = WhisperAdapter(self_hosted_url="http://whisper:9000", openai_api_key="test-key")

    async def fake_post(url: str, **kwargs: object) -> MagicMock:
        raise Exception("All services down")

    with (
        patch("httpx.AsyncClient") as mock_cls,
        patch("builtins.open", mock_open(read_data=b"audio")),
    ):
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_client
        mock_client.post.side_effect = fake_post

        with pytest.raises(Exception, match="All services down"):
            await adapter.transcribe("/tmp/audio.ogg", "ru")
