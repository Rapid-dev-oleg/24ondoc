"""Twenty Integration — REST API Adapter."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from twenty_integration.domain.models import TwentyMember, TwentyPerson, TwentyTask
from twenty_integration.domain.ports import TwentyCRMPort

logger = logging.getLogger(__name__)


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
            items = data.get("data", {}).get("workspaceMembers", [])
            for item in items:
                name = item.get("name", {})
                member = TwentyMember(
                    twenty_id=item.get("id", ""),
                    first_name=name.get("firstName", ""),
                    last_name=name.get("lastName", ""),
                    email=item.get("userEmail", ""),
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

            items = data.get("data", {}).get("people", [])
            if not items:
                return None

            item = items[0]
            person = TwentyPerson(
                twenty_id=item.get("id", ""),
                telegram_id=telegram_id,
                name=item.get("name", {}).get("firstName", ""),
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

            created = data.get("data", {}).get("createPerson", {})
            person = TwentyPerson(
                twenty_id=created.get("id", ""),
                telegram_id=telegram_id,
                name=created.get("name", {}).get("firstName", ""),
            )
            return person
        except httpx.HTTPError as e:
            raise RuntimeError(f"Failed to create person: {e}") from e

    async def fetch_task_field_options(self) -> dict[str, list[dict[str, str]]]:
        """Запросить актуальные списки kategoriya и vazhnost из метаданных Twenty."""
        result: dict[str, list[dict[str, str]]] = {"kategoriya": [], "vazhnost": []}
        try:
            response = await self._client.get("/rest/metadata/objects")
            response.raise_for_status()
            objects = response.json().get("data", {}).get("objects", [])
            for obj in objects:
                if obj.get("nameSingular") != "task":
                    continue
                for fld in obj.get("fields", []):
                    name = fld.get("name", "")
                    if name in ("kategoriya", "vazhnost"):
                        options = fld.get("options", [])
                        result[name] = [
                            {"label": o["label"], "value": o["value"]}
                            for o in options
                            if "label" in o and "value" in o
                        ]
                break
        except Exception:
            logger.exception("Failed to fetch task field options from Twenty metadata")
        logger.info(
            "fetch_task_field_options: kategoriya=%d, vazhnost=%d",
            len(result["kategoriya"]),
            len(result["vazhnost"]),
        )
        return result

    async def create_task(
        self,
        title: str,
        body: str,
        due_at: datetime | None,
        assignee_id: str | None,
        kategoriya: str | None = None,
        vazhnost: str | None = None,
    ) -> TwentyTask:
        """Создать задачу."""
        try:
            payload: dict[str, Any] = {
                "status": "TODO",
                "title": title,
                "bodyV2": {"markdown": body},
            }

            if due_at is not None:
                payload["dueAt"] = due_at.strftime("%Y-%m-%dT%H:%M:%SZ")

            if assignee_id is not None:
                payload["assigneeId"] = assignee_id

            if kategoriya is not None:
                payload["kategoriya"] = kategoriya

            if vazhnost is not None:
                payload["vazhnost"] = vazhnost

            response = await self._client.post("/rest/tasks", json=payload)
            if response.status_code >= 400:
                logger.error(
                    "Twenty create_task failed: %s %s payload=%s",
                    response.status_code,
                    response.text[:300],
                    payload,
                )
            response.raise_for_status()
            data = response.json()

            created = data.get("data", {}).get("createTask", {})
            task = TwentyTask(
                twenty_id=created.get("id", ""),
                title=created.get("title", ""),
                body=created.get("bodyV2", {}).get("markdown", ""),
                status=created.get("status", "TODO"),
                due_at=_parse_datetime(created.get("dueAt")),
                assignee_id=created.get("assigneeId"),
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

    _ATTACHMENT_FILE_FIELD_ID = "0d953c19-1809-41e8-8f78-80d18836bd9d"

    async def upload_file(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str = "application/octet-stream",
    ) -> str | None:
        """Загрузить файл в Twenty через GraphQL multipart upload. Возвращает file ID."""
        import json as _json

        operations = _json.dumps(
            {
                "query": (
                    "mutation UploadFilesFieldFile($file: Upload!, $fieldMetadataId: String!) "
                    "{ uploadFilesFieldFile(file: $file, fieldMetadataId: $fieldMetadataId) "
                    "{ id path } }"
                ),
                "variables": {"file": None, "fieldMetadataId": self._ATTACHMENT_FILE_FIELD_ID},
            }
        )
        files = {
            "operations": (None, operations, "application/json"),
            "map": (None, '{"0":["variables.file"]}', "application/json"),
            "0": (filename, file_bytes, content_type),
        }
        try:
            response = await self._client.post("/metadata", files=files)  # type: ignore[arg-type]
            response.raise_for_status()
            data = response.json()
            uploaded = data.get("data", {}).get("uploadFilesFieldFile", {})
            file_id = uploaded.get("id")
            if not file_id:
                logger.warning("Twenty upload_file: no id in response: %s", data)
            return file_id  # type: ignore[no-any-return]
        except httpx.HTTPError as e:
            logger.warning("Twenty upload_file failed for %s: %s", filename, e)
            return None

    async def create_attachment(self, task_id: str, name: str, uploaded_file_id: str) -> None:
        """Создать attachment с загруженным файлом, привязать к задаче."""
        try:
            payload = {
                "name": name,
                "file": [{"fileId": uploaded_file_id, "label": name}],
                "targetTaskId": task_id,
            }
            response = await self._client.post("/rest/attachments", json=payload)
            if response.status_code >= 400:
                logger.warning(
                    "Twenty create_attachment failed: %s %s",
                    response.status_code,
                    response.text[:300],
                )
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("Failed to create attachment %s: %s", name, e)

    async def update_task_body(self, task_id: str, body: str) -> None:
        """Обновить body задачи."""
        try:
            payload = {"bodyV2": {"markdown": body}}
            response = await self._client.patch(f"/rest/tasks/{task_id}", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("Failed to update task body %s: %s", task_id, e)

    # -- TaskCRMPort protocol methods (stub implementations for migration) --

    async def get_conversations(
        self, assignee_id: str, status: str = "open", page: int = 1
    ) -> list[Any]:
        """Получить задачи пользователя из Twenty CRM."""
        try:
            # Map status: open → TODO/V_RABOTE
            params: dict[str, str] = {
                "filter": f"assigneeId[eq]:{assignee_id}",
                "limit": "20",
            }
            response = await self._client.get("/rest/tasks", params=params)
            response.raise_for_status()
            data = response.json()
            tasks = data.get("data", {}).get("tasks", [])

            from enum import Enum
            from types import SimpleNamespace

            class _Status(Enum):
                TODO = "TODO"
                V_RABOTE = "V_RABOTE"
                VYPOLNENO = "VYPOLNENO"
                KORZINA = "KORZINA"

            result = []
            for t in tasks:
                task_status = t.get("status", "TODO")
                # Filter by requested status
                if status == "open" and task_status not in ("TODO", "V_RABOTE"):
                    continue
                try:
                    s = _Status(task_status)
                except ValueError:
                    s = _Status.TODO
                result.append(
                    SimpleNamespace(
                        task_id=t.get("id", ""),
                        title=t.get("title", ""),
                        status=s,
                        assignee_crm_id=assignee_id,
                    )
                )
            return result
        except Exception:
            logger.exception("Failed to get tasks from Twenty")
            return []

    async def update_task_status(self, task_id: str, status: str) -> None:
        """Update task status in Twenty CRM."""
        payload = {"status": status}
        response = await self._client.patch(f"/rest/tasks/{task_id}", json=payload)
        response.raise_for_status()

    async def update_conversation_status(self, task_id: int, status: str) -> None:
        """Обновить статус задачи (legacy stub)."""

    async def update_conversation_assignee(self, task_id: int, assignee_id: int) -> None:
        """Переназначить задачу (stub)."""

    async def add_message(self, task_id: int, content: str, private: bool = True) -> None:
        """Добавить комментарий к задаче (stub)."""

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
