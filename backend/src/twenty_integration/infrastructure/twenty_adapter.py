"""Twenty Integration — REST API Adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from twenty_integration.domain.models import TwentyMember, TwentyPerson, TwentyTask
from twenty_integration.domain.ports import TwentyCRMPort


class TwentyRestAdapter(TwentyCRMPort):
    """HTTP-клиент для Twenty REST API."""

    def __init__(self, base_url: str, api_key: str) -> None:
        """Инициализировать адаптер.

        Args:
            base_url: Base URL Twenty API (e.g., https://api.twenty.com)
            api_key: API ключ для аутентификации
        """
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

    async def list_workspace_members(self) -> list[TwentyMember]:
        """Получить список участников рабочего пространства."""
        try:
            response = await self._client.get("/rest/workspaceMembers")
            response.raise_for_status()
            data = response.json()

            members = []
            edges = data.get("edges", [])
            for edge in edges:
                node = edge.get("node", {})
                member = TwentyMember(
                    twenty_id=node.get("id", ""),
                    first_name=node.get("firstName", ""),
                    last_name=node.get("lastName", ""),
                    email=node.get("email", ""),
                )
                members.append(member)
            return members
        except httpx.HTTPError:
            return []

    async def find_person_by_telegram_id(self, telegram_id: int) -> TwentyPerson | None:
        """Найти контакт по Telegram ID."""
        try:
            response = await self._client.get(
                "/rest/people",
                params={"filter": f"telegramid[eq]:{telegram_id}"},
            )
            response.raise_for_status()
            data = response.json()

            edges = data.get("edges", [])
            if not edges:
                return None

            node = edges[0].get("node", {})
            person = TwentyPerson(
                twenty_id=node.get("id", ""),
                telegram_id=telegram_id,
                name=node.get("name", {}).get("firstName", ""),
            )
            return person
        except httpx.HTTPError:
            return None

    async def create_person(self, telegram_id: int, name: str) -> TwentyPerson:
        """Создать контакт."""
        try:
            payload = {
                "name": {"firstName": name},
                "telegramid": str(telegram_id),
            }
            response = await self._client.post("/rest/people", json=payload)
            response.raise_for_status()
            data = response.json()

            node = data.get("data", {}).get("node", {})
            person = TwentyPerson(
                twenty_id=node.get("id", ""),
                telegram_id=telegram_id,
                name=node.get("name", {}).get("firstName", ""),
            )
            return person
        except httpx.HTTPError as e:
            raise RuntimeError(f"Failed to create person: {e}") from e

    async def create_task(
        self,
        title: str,
        body: str,
        due_at: datetime | None,
        assignee_id: str | None,
    ) -> TwentyTask:
        """Создать задачу."""
        try:
            payload: dict[str, Any] = {
                "status": "TODO",
                "title": title,
                "bodyV2": {"markdown": body},
            }

            if due_at is not None:
                payload["dueAt"] = due_at.isoformat()

            if assignee_id is not None:
                payload["assigneeId"] = assignee_id

            response = await self._client.post("/rest/tasks", json=payload)
            response.raise_for_status()
            data = response.json()

            node = data.get("data", {}).get("node", {})
            task = TwentyTask(
                twenty_id=node.get("id", ""),
                title=node.get("title", ""),
                body=node.get("bodyV2", {}).get("markdown", ""),
                status=node.get("status", "TODO"),
                due_at=_parse_datetime(node.get("dueAt")),
                assignee_id=node.get("assigneeId"),
                person_id=None,
            )
            return task
        except httpx.HTTPError as e:
            raise RuntimeError(f"Failed to create task: {e}") from e

    async def link_person_to_task(self, task_id: str, person_id: str) -> None:
        """Связать контакт с задачей."""
        try:
            payload = {
                "taskId": task_id,
                "targetPersonId": person_id,
            }
            response = await self._client.post("/rest/taskTargets", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"Failed to link person to task: {e}") from e

    async def close(self) -> None:
        """Закрыть HTTP клиент."""
        await self._client.aclose()


def _parse_datetime(value: str | None) -> datetime | None:
    """Парсить ISO datetime строку."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
