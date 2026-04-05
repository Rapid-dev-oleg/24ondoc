"""ATS2 REST Client — HTTP adapter for ATS2 T2 API."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from ats_processing.application.ports import ATS2CallSourcePort

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60.0


class ATS2AuthManager:
    """Manages ATS2 access/refresh tokens with auto-refresh on 403."""

    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        base_url: str,
        proxy_url: str = "",
        env_file_path: str = ".env",
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._base_url = base_url.rstrip("/")
        self._proxy_url = proxy_url or None
        self._env_file_path = env_file_path

    def update_tokens(self, access_token: str, refresh_token: str) -> None:
        """Update tokens in memory (called from admin UI)."""
        self._access_token = access_token
        self._refresh_token = refresh_token
        logger.info("ATS2 tokens updated manually")

    def update_proxy(self, proxy_url: str) -> None:
        """Update proxy URL in memory."""
        self._proxy_url = proxy_url or None
        logger.info("ATS2 auth proxy updated")

    async def get_access_token(self) -> str:
        return self._access_token

    async def refresh(self) -> str:
        """Refresh the access token using the refresh token."""
        url = f"{self._base_url}/authorization/refresh/token"
        async with httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT,
            proxy=self._proxy_url,
        ) as client:
            response = await client.put(
                url,
                headers={
                    "Authorization": self._refresh_token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            data = response.json()
            self._access_token = data["accessToken"]
            self._refresh_token = data["refreshToken"]
            logger.info("ATS2 tokens refreshed successfully")
            self._persist_tokens()
            return self._access_token

    def _persist_tokens(self) -> None:
        """Save current tokens to .env file so they survive restarts."""
        import os

        path = (
            self._env_file_path
            if os.path.isabs(self._env_file_path)
            else f"/app/{self._env_file_path}"
        )
        if not os.path.exists(path):
            logger.warning("Cannot persist ATS2 tokens: %s not found", path)
            return
        try:
            with open(path) as f:
                lines = f.readlines()

            updates = {
                "ATS2_ACCESS_TOKEN": self._access_token,
                "ATS2_REFRESH_TOKEN": self._refresh_token,
            }
            for i, line in enumerate(lines):
                key = line.split("=", 1)[0]
                if key in updates:
                    lines[i] = f"{key}={updates.pop(key)}\n"
            for key, val in updates.items():
                lines.append(f"{key}={val}\n")

            with open(path, "w") as f:
                f.writelines(lines)
            logger.info("ATS2 tokens persisted to %s", path)
        except Exception:
            logger.exception("Failed to persist ATS2 tokens to %s", path)


class ATS2RestClient(ATS2CallSourcePort):
    """HTTP client for ATS2 REST API with proxy support and auto-retry on 403."""

    def __init__(
        self,
        auth_manager: ATS2AuthManager,
        base_url: str,
        proxy_url: str = "",
    ) -> None:
        self._auth = auth_manager
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=_DEFAULT_TIMEOUT,
            proxy=proxy_url or None,
        )

    async def update_proxy(self, proxy_url: str) -> None:
        """Update proxy — recreate HTTP client."""
        await self._http.aclose()
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=_DEFAULT_TIMEOUT,
            proxy=proxy_url or None,
        )
        self._auth.update_proxy(proxy_url)
        logger.info("ATS2 client proxy updated")

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": token,
            "Accept": "application/json",
        }

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

        response = await self._http.request(
            method, path, headers=self._headers(token), params=params, json=json
        )

        if response.status_code == 403:
            new_token = await self._auth.refresh()
            response = await self._http.request(
                method, path, headers=self._headers(new_token), params=params, json=json
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
        headers = self._headers(token)
        headers["Accept"] = "*/*"

        response = await self._http.request(method, path, headers=headers, params=params)

        if response.status_code == 403:
            new_token = await self._auth.refresh()
            headers["Authorization"] = new_token
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
            "start": date_from.isoformat(),
            "end": date_to.isoformat(),
            "is_recorded": "true",
            "size": "50",
            "sort": "date,DESC",
        }
        result = await self._request("GET", "/call-records/info", params=params)
        return result  # type: ignore[no-any-return]

    async def download_recording(self, filename: str) -> bytes:
        """GET /call-records/file?filename= — download MP3 recording."""
        return await self._request_bytes(
            "GET", "/call-records/file", params={"filename": quote(filename, safe="")}
        )

    async def get_transcription(self, filename: str) -> dict[str, object]:
        """GET /call-records/file/stt?filename= — get STT transcription."""
        result = await self._request(
            "GET", "/call-records/file/stt", params={"filename": quote(filename, safe="")}
        )
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
        await self._http.aclose()
