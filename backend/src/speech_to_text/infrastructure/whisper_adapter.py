"""Speech-to-Text — Whisper Adapter (STTPort implementation)."""

from __future__ import annotations

import httpx

from ..domain.repository import STTPort


class WhisperAdapter(STTPort):
    """Реализация STTPort: self-hosted Whisper → fallback OpenAI Whisper API."""

    def __init__(
        self,
        self_hosted_url: str = "http://whisper:9000",
        openai_api_key: str = "",
    ) -> None:
        self._self_hosted_url = self_hosted_url.rstrip("/")
        self._openai_api_key = openai_api_key

    async def transcribe(self, audio_file_path: str, language: str = "ru") -> str:
        if self._self_hosted_url:
            try:
                return await self._transcribe_self_hosted(audio_file_path, language)
            except Exception:
                pass

        return await self._transcribe_openai(audio_file_path, language)

    async def _transcribe_self_hosted(self, audio_file_path: str, language: str) -> str:
        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(audio_file_path, "rb") as f:
                response = await client.post(
                    f"{self._self_hosted_url}/asr",
                    files={"audio_file": f},
                    params={"language": language, "output": "txt"},
                )
                response.raise_for_status()
                return response.text.strip()

    async def _transcribe_openai(self, audio_file_path: str, language: str) -> str:
        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(audio_file_path, "rb") as f:
                response = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self._openai_api_key}"},
                    files={"file": f},
                    data={"model": "whisper-1", "language": language},
                )
                response.raise_for_status()
                return str(response.json()["text"])
