"""Chatwoot Integration — HTTP client implementing ChatwootPort."""

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

from chatwoot_integration.domain.models import (
    CreateTicketCommand,
    SupportTicket,
    TicketStatus,
)
from chatwoot_integration.domain.repository import ChatwootPort

logger = logging.getLogger(__name__)

_FAILED_QUEUE = "chatwoot:failed_queue"
_MAX_ATTEMPTS = 3


class _ChatwootAPIError(Exception):
    """Ошибка HTTP-ответа от Chatwoot (4xx/5xx)."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Chatwoot API error {status_code}: {body}")
        self.status_code = status_code


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, _ChatwootAPIError):
        return exc.status_code >= 500
    if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError)):
        return True
    return False


class ChatwootClient(ChatwootPort):
    """Реализация ChatwootPort через httpx + tenacity retry + Redis fallback."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        account_id: int,
        redis: AsyncRedis,
        inbox_id: int = 1,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._account_id = account_id
        self._inbox_id = inbox_id
        self._redis: AsyncRedis = redis
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "api_access_token": api_key,
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

    @property
    def _api_prefix(self) -> str:
        return f"/api/v1/accounts/{self._account_id}"

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise _ChatwootAPIError(response.status_code, response.text)

    async def _push_to_queue(self, payload: dict[str, Any]) -> None:
        try:
            await self._redis.rpush(_FAILED_QUEUE, json.dumps(payload))  # type: ignore[misc]
        except Exception:
            logger.exception("Failed to push to Redis queue: %s", payload)

    async def create_conversation(self, command: CreateTicketCommand) -> SupportTicket | None:  # type: ignore[override]
        """POST /api/v1/accounts/{id}/conversations с retry 3 раза → Redis при неудаче."""
        body: dict[str, Any] = {
            "inbox_id": self._inbox_id,
            "subject": command.title,
            "additional_attributes": {
                "description": command.description,
                "category": command.category,
                "deadline": command.deadline,
            },
            "labels": command.labels,
        }
        if command.assignee_chatwoot_id is not None:
            body["assignee_id"] = command.assignee_chatwoot_id

        attempt = 0

        async def _do_request() -> SupportTicket:
            nonlocal attempt
            attempt += 1
            response = await self._http.post(
                f"{self._api_prefix}/conversations",
                content=json.dumps(body),
            )
            self._raise_for_status(response)
            data: dict[str, Any] = response.json()
            return SupportTicket(
                task_id=data["id"],
                source_session_id=command.source_session_id,
                status=TicketStatus(data.get("status", "open")),
                priority=command.priority,
                title=command.title,
            )

        retry_decorator = retry(
            retry=retry_if_exception_type((_ChatwootAPIError, httpx.TransportError)),
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=wait_exponential(multiplier=0.1, min=0.1, max=1),
            reraise=True,
        )
        decorated = retry_decorator(_do_request)

        try:
            result: SupportTicket = await decorated()
            return result
        except Exception:
            logger.warning("create_conversation failed after %d attempts, queuing.", attempt)
            await self._push_to_queue(
                {
                    "action": "create_conversation",
                    "command": command.model_dump(mode="json"),
                }
            )
            return None

    async def update_conversation_status(self, task_id: int, status: str) -> None:
        """PATCH /api/v1/accounts/{id}/conversations/{task_id} с retry → Redis."""
        body = {"status": status}
        attempt = 0

        async def _do_request() -> None:
            nonlocal attempt
            attempt += 1
            response = await self._http.patch(
                f"{self._api_prefix}/conversations/{task_id}",
                content=json.dumps(body),
            )
            self._raise_for_status(response)

        retry_decorator = retry(
            retry=retry_if_exception_type((_ChatwootAPIError, httpx.TransportError)),
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=wait_exponential(multiplier=0.1, min=0.1, max=1),
            reraise=True,
        )
        decorated = retry_decorator(_do_request)

        try:
            await decorated()
        except Exception:
            logger.warning("update_conversation_status failed after %d attempts, queuing.", attempt)
            await self._push_to_queue(
                {
                    "action": "update_conversation_status",
                    "task_id": task_id,
                    "status": status,
                }
            )

    async def get_conversations(
        self,
        assignee_id: int,
        status: str = "open",
        page: int = 1,
    ) -> list[SupportTicket]:
        """GET /api/v1/accounts/{id}/conversations — возвращает [] при ошибке."""
        params: dict[str, str | int] = {"status": status, "page": page, "assignee_type": "assigned"}
        try:
            response = await self._http.get(
                f"{self._api_prefix}/conversations",
                params=params,
            )
            self._raise_for_status(response)
            data: dict[str, Any] = response.json()
            payload: list[dict[str, Any]] = data.get("data", {}).get("payload", [])
            return [
                SupportTicket(
                    task_id=item["id"],
                    status=TicketStatus(item.get("status", "open")),
                    title=item.get("meta", {}).get("subject", ""),
                    assignee_chatwoot_id=(
                        item.get("meta", {}).get("assignee", {}).get("id")
                        if item.get("meta", {}).get("assignee")
                        else None
                    ),
                )
                for item in payload
            ]
        except Exception:
            logger.warning("get_conversations failed for assignee=%d", assignee_id)
            return []

    async def update_conversation_assignee(self, task_id: int, assignee_chatwoot_id: int) -> None:
        """POST /api/v1/accounts/{id}/conversations/{task_id}/assignments с retry → Redis."""
        body = {"assignee_id": assignee_chatwoot_id}
        attempt = 0

        async def _do_request() -> None:
            nonlocal attempt
            attempt += 1
            response = await self._http.post(
                f"{self._api_prefix}/conversations/{task_id}/assignments",
                content=json.dumps(body),
            )
            self._raise_for_status(response)

        retry_decorator = retry(
            retry=retry_if_exception_type((_ChatwootAPIError, httpx.TransportError)),
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=wait_exponential(multiplier=0.1, min=0.1, max=1),
            reraise=True,
        )
        decorated = retry_decorator(_do_request)

        try:
            await decorated()
        except Exception:
            logger.warning(
                "update_conversation_assignee failed after %d attempts, queuing.", attempt
            )
            await self._push_to_queue(
                {
                    "action": "update_conversation_assignee",
                    "task_id": task_id,
                    "assignee_chatwoot_id": assignee_chatwoot_id,
                }
            )

    async def add_message(self, task_id: int, content: str, private: bool = True) -> None:
        """POST /api/v1/accounts/{id}/conversations/{task_id}/messages с retry → Redis."""
        body = {"content": content, "message_type": "outgoing", "private": private}
        attempt = 0

        async def _do_request() -> None:
            nonlocal attempt
            attempt += 1
            response = await self._http.post(
                f"{self._api_prefix}/conversations/{task_id}/messages",
                content=json.dumps(body),
            )
            self._raise_for_status(response)

        retry_decorator = retry(
            retry=retry_if_exception_type((_ChatwootAPIError, httpx.TransportError)),
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=wait_exponential(multiplier=0.1, min=0.1, max=1),
            reraise=True,
        )
        decorated = retry_decorator(_do_request)

        try:
            await decorated()
        except Exception:
            logger.warning("add_message failed after %d attempts, queuing.", attempt)
            await self._push_to_queue(
                {
                    "action": "add_message",
                    "task_id": task_id,
                    "content": content,
                    "private": private,
                }
            )
