"""Chatwoot Integration — Application Use Cases."""

from __future__ import annotations

from chatwoot_integration.domain.models import CreateTicketCommand, SupportTicket
from chatwoot_integration.domain.repository import ChatwootPort, SupportTicketRepository
from telegram_ingestion.domain.models import DraftSession


class CreateTicketFromSession:
    """Use Case: создать задачу в Chatwoot из завершённой DraftSession (статус PREVIEW)."""

    def __init__(
        self,
        chatwoot_port: ChatwootPort,
        ticket_repo: SupportTicketRepository,
    ) -> None:
        self._chatwoot = chatwoot_port
        self._repo = ticket_repo

    async def execute(
        self, session: DraftSession, contact_id: int | None = None
    ) -> SupportTicket | None:
        if session.ai_result is None:
            return None

        command = CreateTicketCommand(
            title=session.ai_result.title,
            description=session.ai_result.description,
            priority=session.ai_result.priority,
            category=session.ai_result.category,
            deadline=session.ai_result.deadline,
            source_session_id=session.session_id,
            contact_id=contact_id,
        )

        ticket = await self._chatwoot.create_conversation(command)
        if ticket is None:
            return None

        await self._repo.save(ticket)
        return ticket
