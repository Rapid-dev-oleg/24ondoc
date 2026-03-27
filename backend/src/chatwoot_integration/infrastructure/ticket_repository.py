"""Chatwoot Integration — In-memory SupportTicketRepository implementation."""
from __future__ import annotations

from ..domain.models import SupportTicket
from ..domain.repository import SupportTicketRepository


class InMemorySupportTicketRepository(SupportTicketRepository):
    """Thread-safe in-memory store for SupportTicket aggregates.

    Suitable for single-process deployments. Tickets mirror Chatwoot state
    and are rebuilt from Chatwoot API on restart if needed.
    """

    def __init__(self) -> None:
        self._store: dict[int, SupportTicket] = {}

    async def get_by_id(self, task_id: int) -> SupportTicket | None:
        return self._store.get(task_id)

    async def save(self, ticket: SupportTicket) -> None:
        self._store[ticket.task_id] = ticket

    async def get_by_assignee(
        self, telegram_id: int, status: str | None = None
    ) -> list[SupportTicket]:
        results = [
            t for t in self._store.values() if t.assignee_telegram_id == telegram_id
        ]
        if status:
            results = [t for t in results if t.status.value == status]
        return results
