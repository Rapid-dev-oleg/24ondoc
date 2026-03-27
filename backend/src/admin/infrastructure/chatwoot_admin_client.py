"""Admin panel — Chatwoot admin client for creating agents."""

from __future__ import annotations

import json
import logging

import httpx

from admin.application.ports import ChatwootAdminPort

logger = logging.getLogger(__name__)


class ChatwootAdminClient(ChatwootAdminPort):
    """Creates agents in Chatwoot via the admin REST API."""

    def __init__(self, base_url: str, api_key: str, account_id: int) -> None:
        self._account_id = account_id
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "api_access_token": api_key,
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

    async def create_agent(self, name: str, email: str, role: str) -> int:
        """POST /api/v1/accounts/{id}/agents and return the new chatwoot_user_id."""
        chatwoot_role = "administrator" if role == "admin" else "agent"
        body = {"name": name, "email": email, "role": chatwoot_role}
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
