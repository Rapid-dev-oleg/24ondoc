"""Тесты для TwentyRestAdapter."""

from __future__ import annotations

from datetime import datetime
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from twenty_integration.infrastructure.twenty_adapter import TwentyRestAdapter


@pytest.fixture
async def adapter() -> AsyncGenerator[TwentyRestAdapter, None]:
    """Создать адаптер для тестирования."""
    adapter_instance = TwentyRestAdapter(
        base_url="https://api.twenty.com",
        api_key="test-api-key",
    )
    yield adapter_instance
    await adapter_instance.close()


@pytest.mark.asyncio
async def test_twenty_adapter_lists_workspace_members(adapter: TwentyRestAdapter) -> None:
    """Должен получить список участников рабочего пространства."""
    response_data: dict[str, Any] = {
        "edges": [
            {
                "node": {
                    "id": "member-uuid-1",
                    "firstName": "Ivan",
                    "lastName": "Ivanov",
                    "email": "ivan@example.com",
                }
            },
            {
                "node": {
                    "id": "member-uuid-2",
                    "firstName": "Maria",
                    "lastName": "Petrova",
                    "email": "maria@example.com",
                }
            },
        ]
    }

    with patch.object(adapter._client, "get", new_callable=AsyncMock) as mock_get:
        mock_response = MagicMock()
        mock_response.json.return_value = response_data
        mock_get.return_value = mock_response

        result = await adapter.list_workspace_members()

        assert len(result) == 2
        assert result[0].twenty_id == "member-uuid-1"
        assert result[0].first_name == "Ivan"
        assert result[0].last_name == "Ivanov"
        assert result[0].email == "ivan@example.com"

        mock_get.assert_called_once_with("/rest/workspaceMembers")


@pytest.mark.asyncio
async def test_twenty_adapter_find_person_by_telegram_id(adapter: TwentyRestAdapter) -> None:
    """Должен найти контакт по Telegram ID."""
    telegram_id = 123456789
    response_data = {
        "edges": [
            {
                "node": {
                    "id": "person-uuid-1",
                    "name": {"firstName": "Ivan"},
                }
            }
        ]
    }

    with patch.object(adapter._client, "get", new_callable=AsyncMock) as mock_get:
        mock_response = MagicMock()
        mock_response.json.return_value = response_data
        mock_get.return_value = mock_response

        result = await adapter.find_person_by_telegram_id(telegram_id)

        assert result is not None
        assert result.twenty_id == "person-uuid-1"
        assert result.telegram_id == telegram_id
        assert result.name == "Ivan"

        mock_get.assert_called_once_with(
            "/rest/people",
            params={"filter": f"telegramid[eq]:{telegram_id}"},
        )


@pytest.mark.asyncio
async def test_twenty_adapter_find_person_by_telegram_id_returns_none_when_not_found(
    adapter: TwentyRestAdapter,
) -> None:
    """Должен вернуть None если контакт не найден."""
    telegram_id = 987654321
    response_data: dict[str, Any] = {"edges": []}

    with patch.object(adapter._client, "get", new_callable=AsyncMock) as mock_get:
        mock_response = MagicMock()
        mock_response.json.return_value = response_data
        mock_get.return_value = mock_response

        result = await adapter.find_person_by_telegram_id(telegram_id)

        assert result is None


@pytest.mark.asyncio
async def test_twenty_adapter_posts_to_people_endpoint(adapter: TwentyRestAdapter) -> None:
    """Должен создать контакт через POST /rest/people."""
    telegram_id = 123456789
    name = "Ivan Ivanov"
    response_data = {
        "data": {
            "node": {
                "id": "person-uuid-1",
                "name": {"firstName": name},
            }
        }
    }

    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.json.return_value = response_data
        mock_post.return_value = mock_response

        result = await adapter.create_person(telegram_id, name)

        assert result.twenty_id == "person-uuid-1"
        assert result.telegram_id == telegram_id
        assert result.name == name

        # Убедимся, что POST был вызван с правильным телом
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "/rest/people"
        json_payload = call_args[1]["json"]
        assert json_payload == {
            "name": {"firstName": name},
            "telegramid": str(telegram_id),
        }


@pytest.mark.asyncio
async def test_twenty_adapter_posts_to_tasks_endpoint(adapter: TwentyRestAdapter) -> None:
    """Должен создать задачу через POST /rest/tasks."""
    title = "Test Task"
    body = "Task description"
    due_at = datetime.fromisoformat("2026-04-15T10:30:00")
    assignee_id = "member-uuid-1"

    response_data = {
        "data": {
            "node": {
                "id": "task-uuid-1",
                "title": title,
                "bodyV2": {"markdown": body},
                "status": "TODO",
                "dueAt": due_at.isoformat(),
                "assigneeId": assignee_id,
            }
        }
    }

    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.json.return_value = response_data
        mock_post.return_value = mock_response

        result = await adapter.create_task(title, body, due_at, assignee_id)

        assert result.twenty_id == "task-uuid-1"
        assert result.title == title
        assert result.body == body
        assert result.due_at == due_at
        assert result.assignee_id == assignee_id

        # Убедимся, что POST был вызван с правильным телом
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "/rest/tasks"
        json_payload = call_args[1]["json"]
        assert json_payload == {
            "status": "TODO",
            "title": title,
            "bodyV2": {"markdown": body},
            "dueAt": due_at.isoformat(),
            "assigneeId": assignee_id,
        }


@pytest.mark.asyncio
async def test_twenty_adapter_posts_to_tasks_without_optional_fields(
    adapter: TwentyRestAdapter,
) -> None:
    """Должен создать задачу без optional полей (due_at, assignee_id)."""
    title = "Test Task"
    body = "Task description"

    response_data = {
        "data": {
            "node": {
                "id": "task-uuid-1",
                "title": title,
                "bodyV2": {"markdown": body},
                "status": "TODO",
            }
        }
    }

    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.json.return_value = response_data
        mock_post.return_value = mock_response

        result = await adapter.create_task(title, body, None, None)

        assert result.twenty_id == "task-uuid-1"
        assert result.due_at is None
        assert result.assignee_id is None

        # Убедимся, что POST был вызван без optional полей
        json_payload = mock_post.call_args[1]["json"]
        assert "dueAt" not in json_payload
        assert "assigneeId" not in json_payload


@pytest.mark.asyncio
async def test_twenty_adapter_links_person_to_task(adapter: TwentyRestAdapter) -> None:
    """Должен связать контакт с задачей через POST /rest/taskTargets."""
    task_id = "task-uuid-1"
    person_id = "person-uuid-1"

    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_post.return_value = mock_response

        await adapter.link_person_to_task(task_id, person_id)

        # Убедимся, что POST был вызван с правильным телом
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "/rest/taskTargets"
        json_payload = call_args[1]["json"]
        assert json_payload == {
            "taskId": task_id,
            "targetPersonId": person_id,
        }


@pytest.mark.asyncio
async def test_twenty_adapter_handles_http_error_gracefully(adapter: TwentyRestAdapter) -> None:
    """Должен обработать HTTP ошибки при получении members."""
    with patch.object(adapter._client, "get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = httpx.HTTPError("Connection error")

        result = await adapter.list_workspace_members()

        # Должен вернуть пустой список при ошибке
        assert result == []


@pytest.mark.asyncio
async def test_twenty_adapter_handles_http_error_on_create_person(
    adapter: TwentyRestAdapter,
) -> None:
    """Должен выбросить RuntimeError при ошибке создания контакта."""
    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.HTTPError("API error")

        with pytest.raises(RuntimeError, match="Failed to create person"):
            await adapter.create_person(123456789, "Ivan")


@pytest.mark.asyncio
async def test_twenty_adapter_handles_http_error_on_create_task(
    adapter: TwentyRestAdapter,
) -> None:
    """Должен выбросить RuntimeError при ошибке создания задачи."""
    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.HTTPError("API error")

        with pytest.raises(RuntimeError, match="Failed to create task"):
            await adapter.create_task("Title", "Body", None, None)


@pytest.mark.asyncio
async def test_twenty_adapter_handles_http_error_on_link_person_to_task(
    adapter: TwentyRestAdapter,
) -> None:
    """Должен выбросить RuntimeError при ошибке связывания контакта с задачей."""
    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.HTTPError("API error")

        with pytest.raises(RuntimeError, match="Failed to link person to task"):
            await adapter.link_person_to_task("task-id", "person-id")
