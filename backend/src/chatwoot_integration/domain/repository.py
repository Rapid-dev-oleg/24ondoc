"""Chatwoot Integration — Abstract Repository and Port."""
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import CreateTicketCommand, SupportTicket


class SupportTicketRepository(ABC):
    @abstractmethod
    async def get_by_id(self, task_id: int) -> SupportTicket | None: ...

    @abstractmethod
    async def save(self, ticket: SupportTicket) -> None: ...

    @abstractmethod
    async def get_by_assignee(
        self, telegram_id: int, status: str | None = None
    ) -> list[SupportTicket]: ...


class ChatwootPort(ABC):
    """Anti-Corruption Layer: интерфейс к Chatwoot API."""

    @abstractmethod
    async def create_conversation(self, command: CreateTicketCommand) -> SupportTicket: ...

    @abstractmethod
    async def update_conversation_status(self, task_id: int, status: str) -> None: ...

    @abstractmethod
    async def get_conversations(
        self, assignee_id: int, status: str = "open", page: int = 1
    ) -> list[SupportTicket]: ...

    @abstractmethod
    async def add_message(self, task_id: int, content: str, private: bool = True) -> None: ...

    @abstractmethod
    async def update_conversation_assignee(
        self, task_id: int, assignee_chatwoot_id: int
    ) -> None: ...
