"""ATS Processing — MinIO AudioStoragePort adapter."""
from __future__ import annotations

import io
import logging
from typing import Any

from ..application.ports import AudioStoragePort

logger = logging.getLogger(__name__)


class MinIOAudioStorage(AudioStoragePort):
    """Stores audio files in MinIO using the minio-py async client."""

    def __init__(
        self,
        minio_client: Any,
        bucket: str = "voice-samples",
        presigned_url_expiry: int = 3600,
    ) -> None:
        self._client = minio_client
        self._bucket = bucket
        self._presigned_expiry = presigned_url_expiry

    async def upload(self, key: str, data: bytes, content_type: str = "audio/ogg") -> str:
        """Upload bytes to MinIO under key. Returns the MinIO path."""
        stream = io.BytesIO(data)
        try:
            await self._client.put_object(
                self._bucket,
                key,
                stream,
                length=len(data),
                content_type=content_type,
            )
        except Exception:
            logger.exception("MinIO upload failed for key: %s", key)
            raise
        return f"{self._bucket}/{key}"

    async def get_presigned_url(self, key: str) -> str:
        """Return a presigned download URL for key."""
        try:
            url: str = await self._client.presigned_get_object(self._bucket, key)
            return url
        except Exception:
            logger.exception("MinIO presign failed for key: %s", key)
            raise
