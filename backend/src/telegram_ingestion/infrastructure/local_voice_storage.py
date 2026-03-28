"""Telegram Ingestion — Local filesystem voice sample storage."""

from __future__ import annotations

import os

import aiofiles

from ..application.ports import VoiceSampleStoragePort


class LocalVoiceSampleStorage(VoiceSampleStoragePort):
    """Saves voice sample bytes to a local directory.

    Filename pattern: ``<base_dir>/<telegram_id>.<ext>``
    Each new upload overwrites the previous sample for that user.
    """

    def __init__(self, base_dir: str) -> None:
        self._base_dir = base_dir

    async def save(self, telegram_id: int, data: bytes, ext: str) -> str:
        os.makedirs(self._base_dir, exist_ok=True)
        filename = f"{telegram_id}.{ext}"
        path = os.path.join(self._base_dir, filename)
        async with aiofiles.open(path, "wb") as f:
            await f.write(data)
        return path
