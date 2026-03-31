"""Telegram Ingestion — STTPort adapter: self-hosted Whisper with OpenAI fallback."""

from __future__ import annotations

import logging
import os
import tempfile

import httpx

from ..application.ports import STTPort

logger = logging.getLogger(__name__)


class OpenRouterSTTAdapter(STTPort):
    """Transcribes audio bytes via self-hosted Whisper, falling back to OpenAI API.

    Primary: self-hosted ``openai-whisper-asr-webservice`` at *whisper_url* (``/asr``).
    Fallback: OpenAI-compatible ``/audio/transcriptions`` at *openai_base_url*.
    """

    def __init__(
        self,
        api_key: str,
        whisper_url: str = "",
        openai_base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self._api_key = api_key
        self._whisper_url = whisper_url.rstrip("/") if whisper_url else ""
        self._openai_base_url = openai_base_url.rstrip("/")

    async def transcribe(self, file_bytes: bytes) -> str:
        if self._whisper_url:
            try:
                return await self._transcribe_self_hosted(file_bytes)
            except Exception:
                logger.warning(
                    "Self-hosted Whisper failed at %s, falling back to OpenAI API",
                    self._whisper_url,
                    exc_info=True,
                )

        return await self._transcribe_openai(file_bytes)

    async def _transcribe_self_hosted(self, file_bytes: bytes) -> str:
        """Call self-hosted openai-whisper-asr-webservice /asr endpoint."""
        # Detect format by magic bytes
        is_mp3 = file_bytes[:3] == b"ID3" or (file_bytes[:2] == b"\xff\xfb")
        ext = ".mp3" if is_mp3 else ".ogg"
        mime = "audio/mpeg" if is_mp3 else "audio/ogg"

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
        """Call OpenAI-compatible /audio/transcriptions endpoint."""
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(tmp_path, "rb") as f:
                    response = await client.post(
                        f"{self._openai_base_url}/audio/transcriptions",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        files={"file": ("audio.ogg", f, "audio/ogg")},
                        data={"model": "whisper-1", "language": "ru"},
                    )
                    response.raise_for_status()
                    return str(response.json()["text"])
        finally:
            os.unlink(tmp_path)
