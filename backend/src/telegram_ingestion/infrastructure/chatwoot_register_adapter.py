"""Telegram Ingestion — Chatwoot adapter for agent auto-registration."""

from __future__ import annotations

import json
import logging

import httpx

from ..application.ports import AgentRegistrationPort

logger = logging.getLogger(__name__)


class ChatwootRegisterAdapter(AgentRegistrationPort):
    """Creates an agent in Chatwoot.

    If platform_api_key is provided, uses Platform API
    (user is created via /platform/api/v1/users and added to the account).
    Otherwise, falls back to Application API with the supplied password.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        account_id: int,
        platform_api_key: str | None = None,
    ) -> None:
        self._account_id = account_id
        self._platform_api_key = platform_api_key
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "api_access_token": api_key,
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )
        self._platform_http: httpx.AsyncClient | None = None
        if platform_api_key:
            self._platform_http = httpx.AsyncClient(
                base_url=base_url.rstrip("/"),
                headers={
                    "api_access_token": platform_api_key,
                    "Content-Type": "application/json",
                },
                timeout=10.0,
            )

    async def create_chatwoot_agent(self, name: str, email: str, password: str) -> int:
        """Create agent in Chatwoot and return the external chatwoot_user_id.

        Uses Platform API when platform_api_key is configured.
        Falls back to Application API when platform_api_key is not set.
        """
        if self._platform_api_key and self._platform_http is not None:
            return await self._create_via_platform_api(name, email, password)
        return await self._create_via_application_api(name, email, password)

    async def _create_via_platform_api(self, name: str, email: str, password: str) -> int:
        """POST /platform/api/v1/users then add user to account."""
        assert self._platform_http is not None
        body: dict[str, object] = {
            "name": name,
            "email": email,
            "password": password,
            "role": "agent",
            "confirmed": True,
        }
        response = await self._platform_http.post(
            "/platform/api/v1/users",
            content=json.dumps(body),
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Chatwoot Platform user creation failed {response.status_code}: {response.text}"
            )
        data: dict[str, object] = response.json()
        user_id = int(str(data["id"]))

        account_body: dict[str, object] = {"user_id": user_id, "role": "agent"}
        account_response = await self._platform_http.post(
            f"/platform/api/v1/accounts/{self._account_id}/account_users",
            content=json.dumps(account_body),
        )
        if account_response.status_code >= 400:
            raise RuntimeError(
                f"Failed to add Chatwoot user {user_id} to account {self._account_id}: "
                f"{account_response.status_code} {account_response.text}"
            )
        return user_id

    async def _create_via_application_api(self, name: str, email: str, password: str) -> int:
        """POST /api/v1/accounts/{id}/agents with password field."""
        body: dict[str, object] = {
            "name": name,
            "email": email,
            "role": "agent",
            "password": password,
        }
        response = await self._http.post(
            f"/api/v1/accounts/{self._account_id}/agents",
            content=json.dumps(body),
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Chatwoot agent creation failed {response.status_code}: {response.text}"
            )
        data: dict[str, object] = response.json()
        return int(str(data["id"]))
