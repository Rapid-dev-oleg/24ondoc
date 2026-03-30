"""Telegram Ingestion — Use Cases для /my_tasks flow."""

from __future__ import annotations

from typing import Any, Protocol

from ..domain.models import UserRole
from .ports import UserProfilePort


class TaskCRMPort(Protocol):
    """Minimal protocol for task-management CRM operations used by task use cases."""

    async def get_conversations(
        self, assignee_id: int, status: str = "open", page: int = 1
    ) -> list[Any]: ...

    async def update_conversation_status(self, task_id: int, status: str) -> None: ...

    async def update_conversation_assignee(self, task_id: int, assignee_id: int) -> None: ...

    async def add_message(self, task_id: int, content: str, private: bool = True) -> None: ...


class GetMyTasksUseCase:
    """Получить список открытых задач текущего пользователя."""

    def __init__(self, user_port: UserProfilePort, crm_port: TaskCRMPort) -> None:
        self._user_port = user_port
        self._crm_port = crm_port

    async def execute(self, telegram_id: int, page: int = 1) -> list[Any]:
        profile = await self._user_port.get_profile(telegram_id)
        if profile is None:
            return []
        result: list[Any] = await self._crm_port.get_conversations(
            assignee_id=profile.telegram_id,
            status="open",
            page=page,
        )
        return result


class UpdateTaskStatusUseCase:
    """Изменить статус задачи. Только assignee может менять статус."""

    def __init__(self, user_port: UserProfilePort, crm_port: TaskCRMPort) -> None:
        self._user_port = user_port
        self._crm_port = crm_port

    async def execute(
        self,
        requester_telegram_id: int,
        task_id: int,
        assignee_crm_id: int | None,
        new_status: str,
    ) -> bool:
        if assignee_crm_id is None:
            return False
        profile = await self._user_port.get_profile(requester_telegram_id)
        if profile is None:
            return False
        if profile.telegram_id != assignee_crm_id:
            return False
        await self._crm_port.update_conversation_status(task_id, new_status)
        return True


class ReassignTaskUseCase:
    """Переназначить задачу. Только supervisor или admin может переназначать."""

    def __init__(self, user_port: UserProfilePort, crm_port: TaskCRMPort) -> None:
        self._user_port = user_port
        self._crm_port = crm_port

    async def execute(
        self,
        requester_telegram_id: int,
        task_id: int,
        target_user_id: int,
    ) -> bool:
        profile = await self._user_port.get_profile(requester_telegram_id)
        if profile is None:
            return False
        if profile.role not in (UserRole.SUPERVISOR, UserRole.ADMIN):
            return False
        await self._crm_port.update_conversation_assignee(task_id, target_user_id)
        return True


class AddTaskCommentUseCase:
    """Добавить внутренний (private) комментарий к задаче."""

    def __init__(self, crm_port: TaskCRMPort) -> None:
        self._crm_port = crm_port

    async def execute(self, task_id: int, content: str) -> None:
        await self._crm_port.add_message(task_id, content, private=True)
