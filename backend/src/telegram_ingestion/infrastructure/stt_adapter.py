"""Telegram Ingestion — STTPort adapter: Groq primary, self-hosted Whisper fallback."""

from __future__ import annotations

import logging
import os
import tempfile

import httpx

from ..application.ports import STTPort

logger = logging.getLogger(__name__)


class OpenRouterSTTAdapter(STTPort):
    """Transcribes audio via Groq Whisper API (primary) with self-hosted fallback.

    Priority: Groq API (whisper-large-v3-turbo) → self-hosted Whisper → OpenAI API.
    """

    def __init__(
        self,
        api_key: str,
        whisper_url: str = "",
        openai_base_url: str = "https://api.openai.com/v1",
        groq_api_key: str = "",
    ) -> None:
        self._api_key = api_key
        self._whisper_url = whisper_url.rstrip("/") if whisper_url else ""
        self._openai_base_url = openai_base_url.rstrip("/")
        self._groq_api_key = groq_api_key

    async def transcribe(self, file_bytes: bytes) -> str:
        # 1. Groq API (best quality, free)
        if self._groq_api_key:
            try:
                return await self._transcribe_groq(file_bytes)
            except Exception:
                logger.warning("Groq Whisper failed, falling back", exc_info=True)

        # 2. Self-hosted Whisper
        if self._whisper_url:
            try:
                return await self._transcribe_self_hosted(file_bytes)
            except Exception:
                logger.warning("Self-hosted Whisper failed, falling back", exc_info=True)

        # 3. OpenAI API
        return await self._transcribe_openai(file_bytes)

    def _detect_format(self, file_bytes: bytes) -> tuple[str, str]:
        """Return (extension, mime_type) based on magic bytes."""
        if file_bytes[:3] == b"ID3" or (
            len(file_bytes) > 1
            and file_bytes[0] == 0xFF
            and file_bytes[1] in (0xFB, 0xF3, 0xF2)
        ):
            return ".mp3", "audio/mpeg"
        return ".ogg", "audio/ogg"

    async def _transcribe_groq(self, file_bytes: bytes) -> str:
        """Groq Whisper API — whisper-large-v3-turbo."""
        ext, mime = self._detect_format(file_bytes)
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                with open(tmp_path, "rb") as f:
                    response = await client.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {self._groq_api_key}"},
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

    async def _transcribe_self_hosted(self, file_bytes: bytes) -> str:
        """Self-hosted openai-whisper-asr-webservice /asr endpoint."""
        ext, mime = self._detect_format(file_bytes)
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                with open(tmp_path, "rb") as f:
                    response = await client.post(
                        f"{self._whisper_url}/asr",
                        files={"audio_file": (f"audio{ext}", f, mime)},
                        params={"language": "ru", "output": "txt"},
                    )
                    response.raise_for_status()
                    return response.text.strip()
        finally:
            os.unlink(tmp_path)

    async def _transcribe_openai(self, file_bytes: bytes) -> str:
        """OpenAI-compatible /audio/transcriptions endpoint."""
        ext, mime = self._detect_format(file_bytes)
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(tmp_path, "rb") as f:
                    response = await client.post(
                        f"{self._openai_base_url}/audio/transcriptions",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        files={"file": (f"audio{ext}", f, mime)},
                        data={"model": "whisper-1", "language": "ru"},
                    )
                    response.raise_for_status()
                    return str(response.json()["text"])
        finally:
            os.unlink(tmp_path)
