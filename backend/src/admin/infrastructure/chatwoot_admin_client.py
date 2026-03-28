"""Admin panel — Chatwoot admin client for creating agents."""

from __future__ import annotations

import json
import logging

import httpx

from admin.application.ports import ChatwootAdminPort

logger = logging.getLogger(__name__)


class ChatwootAdminClient(ChatwootAdminPort):
    """Creates agents in Chatwoot via admin REST API.

    If platform_api_key is provided, uses Platform API (no password required —
    user is created via /platform/api/v1/users and added to the account).
    Otherwise, uses Application API /api/v1/accounts/{id}/agents.
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

    async def create_agent(self, name: str, email: str, role: str) -> int:
        """Create an agent in Chatwoot, return chatwoot_user_id.

        Uses Platform API when platform_api_key is configured.
        Falls back to Application API otherwise.
        """
        if self._platform_api_key and self._platform_http is not None:
            return await self._create_via_platform_api(name, email, role)
        return await self._create_via_application_api(name, email, role)

    async def _create_via_platform_api(self, name: str, email: str, role: str) -> int:
        """Platform API: create user without password + add to account."""
        assert self._platform_http is not None
        chatwoot_role = "administrator" if role == "admin" else "agent"
        body: dict[str, object] = {
            "name": name,
            "email": email,
            "role": chatwoot_role,
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

        account_body: dict[str, object] = {"user_id": user_id, "role": chatwoot_role}
        account_response = await self._platform_http.post(
            f"/platform/api/v1/accounts/{self._account_id}/account_users",
            content=json.dumps(account_body),
        )
        if account_response.status_code >= 400:
            logger.warning(
                "Failed to add user %d to account %d: %s",
                user_id,
                self._account_id,
                account_response.text,
            )
        return user_id

    async def _create_via_application_api(self, name: str, email: str, role: str) -> int:
        """Application API: POST /api/v1/accounts/{id}/agents."""
        chatwoot_role = "administrator" if role == "admin" else "agent"
        body: dict[str, object] = {"name": name, "email": email, "role": chatwoot_role}
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

    async def delete_agent(self, chatwoot_user_id: int) -> None:
        """Delete agent via Application API. 404 is silently ignored."""
        response = await self._http.delete(
            f"/api/v1/accounts/{self._account_id}/agents/{chatwoot_user_id}"
        )
        if response.status_code == 404:
            return
        if response.status_code >= 400:
            raise RuntimeError(
                f"Chatwoot agent deletion failed {response.status_code}: {response.text}"
            )
