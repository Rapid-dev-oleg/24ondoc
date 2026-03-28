"""Telegram Ingestion — Chatwoot adapter for agent auto-registration."""

from __future__ import annotations

import json
import logging

import httpx

from ..application.ports import AgentRegistrationPort

logger = logging.getLogger(__name__)


class ChatwootRegisterAdapter(AgentRegistrationPort):
    """Creates an agent in Chatwoot with a direct password (no invite flow)."""

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

    async def create_chatwoot_agent(self, name: str, email: str, password: str) -> int:
        """POST /api/v1/accounts/{id}/agents with password field.

        Returns the new Chatwoot agent id (chatwoot_user_id).
        Raises RuntimeError on HTTP error.
        """
        body = {"name": name, "email": email, "role": "agent", "password": password}
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
