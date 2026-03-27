"""Telegram Ingestion — STTPort adapter over OpenAI-compatible API (OpenRouter)."""
from __future__ import annotations

import os
import tempfile

import httpx

from ..application.ports import STTPort


class OpenRouterSTTAdapter(STTPort):
    """Transcribes audio bytes via OpenAI-compatible /audio/transcriptions endpoint.

    Configured to use OpenRouter (OPENAI_API_KEY = OpenRouter key).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def transcribe(self, file_bytes: bytes) -> str:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(tmp_path, "rb") as f:
                    response = await client.post(
                        f"{self._base_url}/audio/transcriptions",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        files={"file": ("audio.ogg", f, "audio/ogg")},
                        data={"model": "whisper-1", "language": "ru"},
                    )
                    response.raise_for_status()
                    return str(response.json()["text"])
        finally:
            os.unlink(tmp_path)
