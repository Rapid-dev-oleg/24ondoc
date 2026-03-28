"""Chatwoot Integration — Platform API client (passwordless user management)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from redis.asyncio import Redis as AsyncRedis
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from chatwoot_integration.domain.models import ChatwootAgent
from chatwoot_integration.domain.repository import ChatwootPlatformPort

logger = logging.getLogger(__name__)

_PLATFORM_FAILED_QUEUE = "chatwoot:platform_failed_queue"
_MAX_ATTEMPTS = 3


class _PlatformAPIError(Exception):
    """Ошибка HTTP-ответа от Chatwoot Platform API (4xx/5xx)."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Chatwoot Platform API error {status_code}: {body}")
        self.status_code = status_code


class ChatwootPlatformClient(ChatwootPlatformPort):
    """Реализация ChatwootPlatformPort через Platform API Chatwoot с retry + Redis fallback."""

    def __init__(self, base_url: str, platform_api_key: str, redis: AsyncRedis) -> None:
        self._redis: AsyncRedis = redis
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "api_access_token": platform_api_key,
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise _PlatformAPIError(response.status_code, response.text)

    async def _push_to_queue(self, payload: dict[str, Any]) -> None:
        try:
            await self._redis.rpush(_PLATFORM_FAILED_QUEUE, json.dumps(payload))  # type: ignore[misc]
        except Exception:
            logger.exception("Failed to push to Platform Redis queue: %s", payload)

    async def create_user(self, name: str, email: str) -> ChatwootAgent:
        """POST /platform/api/v1/users — создание пользователя без пароля (auto-confirm).

        Raises RuntimeError при провале после всех попыток.
        """
        body: dict[str, Any] = {
            "name": name,
            "email": email,
            "role": "agent",
            "confirmed": True,
        }

        attempt = 0

        async def _do_request() -> ChatwootAgent:
            nonlocal attempt
            attempt += 1
            response = await self._http.post(
                "/platform/api/v1/users",
                content=json.dumps(body),
            )
            self._raise_for_status(response)
            data: dict[str, Any] = response.json()
            return ChatwootAgent(
                user_id=int(str(data["id"])),
                access_token=str(data.get("access_token", "")),
            )

        retry_decorator = retry(
            retry=retry_if_exception_type((_PlatformAPIError, httpx.TransportError)),
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=wait_exponential(multiplier=0.1, min=0.1, max=1),
            reraise=True,
        )
        decorated = retry_decorator(_do_request)

        try:
            result: ChatwootAgent = await decorated()
            return result
        except Exception:
            logger.warning("create_user failed after %d attempts, queuing.", attempt)
            await self._push_to_queue({"action": "create_user", "name": name, "email": email})
            raise

    async def add_to_account(self, user_id: int, account_id: int, role: str = "agent") -> None:
        """POST /platform/api/v1/accounts/{id}/account_users — добавить пользователя в аккаунт."""
        body: dict[str, Any] = {"user_id": user_id, "role": role}

        attempt = 0

        async def _do_request() -> None:
            nonlocal attempt
            attempt += 1
            response = await self._http.post(
                f"/platform/api/v1/accounts/{account_id}/account_users",
                content=json.dumps(body),
            )
            self._raise_for_status(response)

        retry_decorator = retry(
            retry=retry_if_exception_type((_PlatformAPIError, httpx.TransportError)),
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=wait_exponential(multiplier=0.1, min=0.1, max=1),
            reraise=True,
        )
        decorated = retry_decorator(_do_request)

        try:
            await decorated()
        except Exception:
            logger.warning("add_to_account failed after %d attempts, queuing.", attempt)
            await self._push_to_queue(
                {
                    "action": "add_to_account",
                    "user_id": user_id,
                    "account_id": account_id,
                    "role": role,
                }
            )
            raise

    async def get_sso_url(self, user_id: int) -> str:
        """GET /platform/api/v1/users/{id}/login — SSO URL для входа без пароля."""
        attempt = 0

        async def _do_request() -> str:
            nonlocal attempt
            attempt += 1
            response = await self._http.get(f"/platform/api/v1/users/{user_id}/login")
            self._raise_for_status(response)
            data: dict[str, Any] = response.json()
            return str(data.get("url", ""))

        retry_decorator = retry(
            retry=retry_if_exception_type((_PlatformAPIError, httpx.TransportError)),
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=wait_exponential(multiplier=0.1, min=0.1, max=1),
            reraise=True,
        )
        decorated = retry_decorator(_do_request)

        try:
            sso_url: str = await decorated()
            return sso_url
        except Exception:
            logger.warning("get_sso_url failed after %d attempts for user_id=%d", attempt, user_id)
            raise
