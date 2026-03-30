"""ATS2 REST Client — HTTP adapter for ATS2 T2 API."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from ats_processing.application.ports import ATS2CallSourcePort

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0


class ATS2AuthManager:
    """Manages ATS2 access/refresh tokens with auto-refresh on 403."""

    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        base_url: str,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._base_url = base_url.rstrip("/")

    async def get_access_token(self) -> str:
        """Return the current cached access token."""
        return self._access_token

    async def refresh(self) -> str:
        """Refresh the access token using the refresh token.

        Calls PUT /authorization/refresh/token with refresh token in header.
        Returns the new access token.
        """
        url = f"{self._base_url}/authorization/refresh/token"
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            response = await client.put(
                url,
                headers={"Authorization": self._refresh_token},
            )
            response.raise_for_status()
            data = response.json()
            self._access_token = data["access_token"]
            return self._access_token


class ATS2RestClient(ATS2CallSourcePort):
    """HTTP client for ATS2 REST API with auto-retry on 403."""

    def __init__(
        self,
        auth_manager: ATS2AuthManager,
        base_url: str,
    ) -> None:
        self._auth = auth_manager
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=_DEFAULT_TIMEOUT,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Execute an API request with automatic 403 token refresh retry."""
        token = await self._auth.get_access_token()
        headers = {"Authorization": token}

        response = await self._http.request(method, path, headers=headers, params=params, json=json)

        if response.status_code == 403:
            new_token = await self._auth.refresh()
            headers = {"Authorization": new_token}
            response = await self._http.request(
                method, path, headers=headers, params=params, json=json
            )

        response.raise_for_status()
        return response.json()

    async def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> bytes:
        """Execute an API request that returns raw bytes (e.g. MP3)."""
        token = await self._auth.get_access_token()
        headers = {"Authorization": token}

        response = await self._http.request(method, path, headers=headers, params=params)

        if response.status_code == 403:
            new_token = await self._auth.refresh()
            headers = {"Authorization": new_token}
            response = await self._http.request(method, path, headers=headers, params=params)

        response.raise_for_status()
        return response.content

    # ---- ATS2CallSourcePort implementation ----

    async def get_call_records(
        self,
        date_from: datetime,
        date_to: datetime,
    ) -> list[dict[str, object]]:
        """GET /call-records/info with date filtering."""
        params: dict[str, Any] = {
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
        }
        result = await self._request("GET", "/call-records/info", params=params)
        return result  # type: ignore[no-any-return]

    async def download_recording(self, filename: str) -> bytes:
        """GET /call-records/file?filename= — download MP3 recording."""
        return await self._request_bytes("GET", "/call-records/file", params={"filename": filename})

    async def get_transcription(self, filename: str) -> dict[str, object]:
        """GET /call-records/file/stt?filename= — get STT transcription."""
        result = await self._request("GET", "/call-records/file/stt", params={"filename": filename})
        return result  # type: ignore[no-any-return]

    async def get_active_calls(self) -> list[dict[str, object]]:
        """GET /monitoring/calls — current active calls."""
        result = await self._request("GET", "/monitoring/calls")
        return result  # type: ignore[no-any-return]

    async def get_employees(self) -> list[dict[str, object]]:
        """GET /employees — list of employees."""
        result = await self._request("GET", "/employees")
        return result  # type: ignore[no-any-return]

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()
