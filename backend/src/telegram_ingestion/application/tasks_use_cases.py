"""Telegram Ingestion — Use Cases для /my_tasks flow."""

from __future__ import annotations

from chatwoot_integration.domain.models import SupportTicket
from chatwoot_integration.domain.repository import ChatwootPort

from ..domain.models import UserRole
from .ports import UserProfilePort


class GetMyTasksUseCase:
    """Получить список открытых задач текущего пользователя из Chatwoot."""

    def __init__(self, user_port: UserProfilePort, chatwoot_port: ChatwootPort) -> None:
        self._user_port = user_port
        self._chatwoot_port = chatwoot_port

    async def execute(self, telegram_id: int, page: int = 1) -> list[SupportTicket]:
        profile = await self._user_port.get_profile(telegram_id)
        if profile is None:
            return []
        result: list[SupportTicket] = await self._chatwoot_port.get_conversations(
            assignee_id=profile.chatwoot_user_id,
            status="open",
            page=page,
        )
        return result


class UpdateTaskStatusUseCase:
    """Изменить статус задачи. Только assignee может менять статус."""

    def __init__(self, user_port: UserProfilePort, chatwoot_port: ChatwootPort) -> None:
        self._user_port = user_port
        self._chatwoot_port = chatwoot_port

    async def execute(
        self,
        requester_telegram_id: int,
        task_id: int,
        assignee_chatwoot_id: int | None,
        new_status: str,
    ) -> bool:
        if assignee_chatwoot_id is None:
            return False
        profile = await self._user_port.get_profile(requester_telegram_id)
        if profile is None:
            return False
        if profile.chatwoot_user_id != assignee_chatwoot_id:
            return False
        await self._chatwoot_port.update_conversation_status(task_id, new_status)
        return True


class ReassignTaskUseCase:
    """Переназначить задачу. Только supervisor или admin может переназначать."""

    def __init__(self, user_port: UserProfilePort, chatwoot_port: ChatwootPort) -> None:
        self._user_port = user_port
        self._chatwoot_port = chatwoot_port

    async def execute(
        self,
        requester_telegram_id: int,
        task_id: int,
        target_chatwoot_user_id: int,
    ) -> bool:
        profile = await self._user_port.get_profile(requester_telegram_id)
        if profile is None:
            return False
        if profile.role not in (UserRole.SUPERVISOR, UserRole.ADMIN):
            return False
        await self._chatwoot_port.update_conversation_assignee(task_id, target_chatwoot_user_id)
        return True


class AddTaskCommentUseCase:
    """Добавить внутренний (private) комментарий к задаче."""

    def __init__(self, chatwoot_port: ChatwootPort) -> None:
        self._chatwoot_port = chatwoot_port

    async def execute(self, task_id: int, content: str) -> None:
        await self._chatwoot_port.add_message(task_id, content, private=True)
