"""Chatwoot Integration — FastAPI webhook endpoint."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from chatwoot_integration.domain.models import SupportTicket, TicketStatus
from chatwoot_integration.domain.repository import SupportTicketRepository, TelegramNotifyPort

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


async def _handle_status_changed(
    payload: dict[str, Any],
    ticket_repo: SupportTicketRepository,
) -> None:
    """Обрабатывает conversation_status_changed — обновляет статус тикета."""
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


async def _handle_message_created(
    payload: dict[str, Any],
    ticket_repo: SupportTicketRepository,
    telegram_notify: TelegramNotifyPort | None,
) -> None:
    """Обрабатывает message_created — уведомляет назначенного агента в Telegram.

    Уведомляет только при сообщениях от контактов (не от агентов/ботов).
    Фильтр собственных сообщений: sender.type != 'contact' → пропустить.
    """
    sender: dict[str, Any] = payload.get("sender") or {}
    sender_type: str = sender.get("type", "")

    if sender_type != "contact":
        logger.debug("message_created ignored: sender_type=%s", sender_type)
        return

    conversation: dict[str, Any] = payload.get("conversation") or {}
    conversation_id: int | None = conversation.get("id")
    if conversation_id is None:
        logger.warning("message_created without conversation.id: %s", payload)
        return

    ticket = await ticket_repo.get_by_id(conversation_id)
    if ticket is None or ticket.assignee_telegram_id is None:
        logger.debug(
            "message_created: ticket %s not found or no assignee_telegram_id", conversation_id
        )
        return

    if telegram_notify is None:
        logger.debug("message_created: TelegramNotifyPort not configured, skipping notification")
        return

    content: str = payload.get("content") or "..."
    sender_name: str = sender.get("name") or "Контакт"
    message = (
        f"\U0001f4ac Новое сообщение в тикете #{conversation_id}\n"
        f"\U0001f464 {sender_name}: {content}"
    )
    await telegram_notify.notify_agent(ticket.assignee_telegram_id, message)
    logger.info(
        "Notified agent telegram_id=%d about message in ticket %d",
        ticket.assignee_telegram_id,
        conversation_id,
    )


async def _handle_conversation_created(
    payload: dict[str, Any],
    ticket_repo: SupportTicketRepository,
) -> None:
    """Обрабатывает conversation_created — создаёт зеркало тикета для внешних разговоров.

    Пропускает, если тикет уже существует (создан через бот).
    """
    conversation_id: int | None = payload.get("id")
    if conversation_id is None:
        logger.warning("conversation_created without 'id': %s", payload)
        return

    existing = await ticket_repo.get_by_id(conversation_id)
    if existing is not None:
        logger.debug("conversation_created: ticket %d already exists, skipping.", conversation_id)
        return

    meta: dict[str, Any] = payload.get("meta") or {}
    assignee: dict[str, Any] | None = meta.get("assignee")
    assignee_chatwoot_id: int | None = assignee.get("id") if assignee else None

    raw_status: str = payload.get("status", "open")
    ticket_status = _STATUS_MAP.get(raw_status, TicketStatus.OPEN)

    ticket = SupportTicket(
        task_id=conversation_id,
        status=ticket_status,
        assignee_chatwoot_id=assignee_chatwoot_id,
    )
    await ticket_repo.save(ticket)
    logger.info(
        "Mirror ticket created for external conversation %d (assignee_chatwoot_id=%s)",
        conversation_id,
        assignee_chatwoot_id,
    )


async def _handle_conversation_updated(
    payload: dict[str, Any],
    ticket_repo: SupportTicketRepository,
) -> None:
    """Обрабатывает conversation_updated — синхронизирует поля тикета.

    Обновляемые поля: priority, labels, assignee_chatwoot_id.
    """
    conversation_id: int | None = payload.get("id")
    if conversation_id is None:
        logger.warning("conversation_updated without 'id': %s", payload)
        return

    ticket = await ticket_repo.get_by_id(conversation_id)
    if ticket is None:
        logger.info("conversation_updated: ticket %d not found, skipping.", conversation_id)
        return

    meta: dict[str, Any] = payload.get("meta") or {}
    assignee: dict[str, Any] | None = meta.get("assignee")
    assignee_chatwoot_id: int | None = assignee.get("id") if assignee else None

    raw_labels: list[str] | None = payload.get("labels")
    raw_priority: str | None = payload.get("priority")

    ticket.update_fields(
        priority=raw_priority,
        labels=raw_labels,
        assignee_chatwoot_id=assignee_chatwoot_id,
    )
    await ticket_repo.save(ticket)
    logger.info(
        "Ticket %d updated: priority=%s labels=%s assignee_chatwoot_id=%s",
        conversation_id,
        raw_priority,
        raw_labels,
        assignee_chatwoot_id,
    )


async def process_webhook_event(
    payload: dict[str, Any],
    ticket_repo: SupportTicketRepository,
    telegram_notify: TelegramNotifyPort | None = None,
) -> None:
    """Обрабатывает входящее webhook-событие от Chatwoot.

    Поддерживаемые события:
    - conversation_status_changed — обновить статус в SupportTicketRepository
    - message_created — уведомить назначенного агента в Telegram
    - conversation_created — создать зеркало тикета для внешних разговоров
    - conversation_updated — синхронизировать поля тикета
    """
    event: str = payload.get("event", "")

    if event == "conversation_status_changed":
        await _handle_status_changed(payload, ticket_repo)
    elif event == "message_created":
        await _handle_message_created(payload, ticket_repo, telegram_notify)
    elif event == "conversation_created":
        await _handle_conversation_created(payload, ticket_repo)
    elif event == "conversation_updated":
        await _handle_conversation_updated(payload, ticket_repo)
    else:
        logger.debug("Ignoring Chatwoot webhook event: %s", event)


@router.post("/webhook/chatwoot", status_code=status.HTTP_200_OK)
async def chatwoot_webhook(
    request: Request,
) -> dict[str, str]:
    """Webhook endpoint для получения событий от Chatwoot.

    Репозиторий и notify-порт внедряются через request.state (настраивается в lifespan приложения).
    """
    payload: dict[str, Any] = await request.json()

    _verify_token(payload, request)

    ticket_repo: SupportTicketRepository | None = getattr(request.state, "ticket_repo", None)
    if ticket_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SupportTicketRepository not available",
        )

    telegram_notify: TelegramNotifyPort | None = getattr(request.state, "telegram_notify", None)

    await process_webhook_event(
        payload=payload,
        ticket_repo=ticket_repo,
        telegram_notify=telegram_notify,
    )
    return {"status": "ok"}
