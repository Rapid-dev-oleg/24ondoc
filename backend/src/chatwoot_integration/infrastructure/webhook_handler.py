"""Chatwoot Integration — FastAPI webhook endpoint."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from chatwoot_integration.domain.models import TicketStatus
from chatwoot_integration.domain.repository import SupportTicketRepository

logger = logging.getLogger(__name__)

router = APIRouter()

_STATUS_MAP: dict[str, TicketStatus] = {
    "open": TicketStatus.OPEN,
    "pending": TicketStatus.PENDING,
    "resolved": TicketStatus.RESOLVED,
    "snoozed": TicketStatus.SNOOZED,
}


def _verify_token(payload: dict[str, Any], request: Request) -> None:
    """Проверяет токен вебхука из тела или заголовка запроса.

    Chatwoot передаёт токен в поле 'token' тела или заголовке 'X-Chatwoot-Token'.
    Raises HTTPException(401) если токен невалиден или отсутствует.
    """
    expected: str | None = getattr(request.state, "chatwoot_webhook_token", None)
    received = payload.get("token") or request.headers.get("X-Chatwoot-Token")
    if expected is None or received != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Chatwoot webhook token",
        )


async def process_webhook_event(
    payload: dict[str, Any],
    ticket_repo: SupportTicketRepository,
) -> None:
    """Обрабатывает входящее webhook-событие от Chatwoot.

    Поддерживаемые события:
    - conversation_status_changed — обновить статус в SupportTicketRepository
    """
    event: str = payload.get("event", "")

    if event != "conversation_status_changed":
        logger.debug("Ignoring Chatwoot webhook event: %s", event)
        return

    task_id: int | None = payload.get("id")
    new_status_raw: str = payload.get("status", "")

    if task_id is None:
        logger.warning("conversation_status_changed without 'id': %s", payload)
        return

    new_status = _STATUS_MAP.get(new_status_raw)
    if new_status is None:
        logger.warning("Unknown Chatwoot status '%s' for task %d", new_status_raw, task_id)
        return

    ticket = await ticket_repo.get_by_id(task_id)
    if ticket is None:
        logger.info("Ticket %d not found in local repo, skipping.", task_id)
        return

    ticket.update_status(new_status)
    await ticket_repo.save(ticket)
    logger.info("Ticket %d status updated to %s", task_id, new_status)


@router.post("/webhook/chatwoot", status_code=status.HTTP_200_OK)
async def chatwoot_webhook(
    request: Request,
) -> dict[str, str]:
    """Webhook endpoint для получения событий от Chatwoot.

    Репозиторий внедряется через request.state (настраивается в lifespan приложения).
    """
    payload: dict[str, Any] = await request.json()

    _verify_token(payload, request)

    ticket_repo: SupportTicketRepository | None = getattr(request.state, "ticket_repo", None)
    if ticket_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SupportTicketRepository not available",
        )

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)
    return {"status": "ok"}
