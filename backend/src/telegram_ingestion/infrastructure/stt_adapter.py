"""Telegram Ingestion — STTPort adapter: Groq Whisper API."""

from __future__ import annotations

import logging
import os
import tempfile

import httpx

from ..application.ports import STTPort

logger = logging.getLogger(__name__)


class GroqSTTAdapter(STTPort):
    """Transcribes audio via Groq Whisper API (whisper-large-v3-turbo)."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def transcribe(self, file_bytes: bytes) -> str:
        ext, mime = self._detect_format(file_bytes)
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                with open(tmp_path, "rb") as f:
                    response = await client.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        files={"file": (f"audio{ext}", f, mime)},
                        data={
                            "model": "whisper-large-v3-turbo",
                            "language": "ru",
                            "response_format": "text",
                        },
                    )
                    response.raise_for_status()
                    return response.text.strip()
        finally:
            os.unlink(tmp_path)

    def _detect_format(self, file_bytes: bytes) -> tuple[str, str]:
        """Return (extension, mime_type) based on magic bytes."""
        if file_bytes[:3] == b"ID3" or (
            len(file_bytes) > 1 and file_bytes[0] == 0xFF and file_bytes[1] in (0xFB, 0xF3, 0xF2)
        ):
            return ".mp3", "audio/mpeg"
        return ".ogg", "audio/ogg"
