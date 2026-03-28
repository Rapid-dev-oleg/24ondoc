"""Тесты для расширенных вебхуков Chatwoot (Этап 3).

Покрывает события:
- message_created (уведомление агента в Telegram)
- conversation_created (зеркало тикета для внешних разговоров)
- conversation_updated (синхронизация полей)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from chatwoot_integration.domain.models import SupportTicket, TicketStatus
from chatwoot_integration.domain.repository import SupportTicketRepository, TelegramNotifyPort
from chatwoot_integration.infrastructure.webhook_handler import process_webhook_event

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def make_ticket(
    task_id: int = 42,
    assignee_telegram_id: int | None = 1001,
    assignee_chatwoot_id: int | None = 5,
    status: TicketStatus = TicketStatus.OPEN,
    priority: str = "medium",
    labels: list[str] | None = None,
) -> SupportTicket:
    return SupportTicket(
        task_id=task_id,
        assignee_telegram_id=assignee_telegram_id,
        assignee_chatwoot_id=assignee_chatwoot_id,
        status=status,
        priority=priority,
        labels=labels or [],
    )


def make_message_created_payload(
    conversation_id: int = 42,
    sender_type: str = "contact",
    sender_name: str = "Иван Петров",
    content: str = "Добрый день, у меня вопрос",
) -> dict[str, object]:
    return {
        "event": "message_created",
        "id": 100,
        "content": content,
        "sender": {"type": sender_type, "name": sender_name, "id": 10},
        "conversation": {"id": conversation_id, "status": "open"},
    }


def make_conversation_created_payload(
    conversation_id: int = 99,
    status: str = "open",
    assignee_id: int | None = 7,
) -> dict[str, object]:
    assignee: dict[str, object] | None = (
        {"id": assignee_id, "name": "Агент"} if assignee_id is not None else None
    )
    return {
        "event": "conversation_created",
        "id": conversation_id,
        "status": status,
        "inbox_id": 1,
        "meta": {"assignee": assignee},
    }


def make_conversation_updated_payload(
    conversation_id: int = 42,
    priority: str | None = "high",
    labels: list[str] | None = None,
    assignee_id: int | None = 7,
) -> dict[str, object]:
    assignee: dict[str, object] | None = (
        {"id": assignee_id, "name": "Агент"} if assignee_id is not None else None
    )
    payload: dict[str, object] = {
        "event": "conversation_updated",
        "id": conversation_id,
        "meta": {"assignee": assignee},
    }
    if priority is not None:
        payload["priority"] = priority
    if labels is not None:
        payload["labels"] = labels
    return payload


# ---------------------------------------------------------------------------
# message_created: уведомление агента
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_created_notifies_agent() -> None:
    """message_created от контакта → уведомить назначенного агента в Telegram."""
    ticket = make_ticket(task_id=42, assignee_telegram_id=1001)

    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=ticket)

    telegram_notify = AsyncMock(spec=TelegramNotifyPort)

    payload = make_message_created_payload(conversation_id=42, content="Помогите!")

    await process_webhook_event(
        payload=payload, ticket_repo=ticket_repo, telegram_notify=telegram_notify
    )

    ticket_repo.get_by_id.assert_called_once_with(42)
    telegram_notify.notify_agent.assert_called_once()
    call_args = telegram_notify.notify_agent.call_args
    assert call_args[0][0] == 1001
    assert "42" in call_args[0][1]
    assert "Помогите!" in call_args[0][1]


@pytest.mark.asyncio
async def test_message_created_agent_sender_not_notified() -> None:
    """message_created от агента (не контакта) → уведомление НЕ отправляется."""
    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    telegram_notify = AsyncMock(spec=TelegramNotifyPort)

    payload = make_message_created_payload(conversation_id=42, sender_type="agent_bot")

    await process_webhook_event(
        payload=payload, ticket_repo=ticket_repo, telegram_notify=telegram_notify
    )

    ticket_repo.get_by_id.assert_not_called()
    telegram_notify.notify_agent.assert_not_called()


@pytest.mark.asyncio
async def test_message_created_user_sender_not_notified() -> None:
    """message_created от user-агента → уведомление НЕ отправляется."""
    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    telegram_notify = AsyncMock(spec=TelegramNotifyPort)

    payload = make_message_created_payload(conversation_id=42, sender_type="user")

    await process_webhook_event(
        payload=payload, ticket_repo=ticket_repo, telegram_notify=telegram_notify
    )

    ticket_repo.get_by_id.assert_not_called()
    telegram_notify.notify_agent.assert_not_called()


@pytest.mark.asyncio
async def test_message_created_ticket_not_found() -> None:
    """message_created: тикет не найден → уведомление НЕ отправляется."""
    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=None)

    telegram_notify = AsyncMock(spec=TelegramNotifyPort)

    payload = make_message_created_payload(conversation_id=99)

    await process_webhook_event(
        payload=payload, ticket_repo=ticket_repo, telegram_notify=telegram_notify
    )

    ticket_repo.get_by_id.assert_called_once_with(99)
    telegram_notify.notify_agent.assert_not_called()


@pytest.mark.asyncio
async def test_message_created_no_assignee_telegram_id() -> None:
    """message_created: у тикета нет assignee_telegram_id → уведомление НЕ отправляется."""
    ticket = make_ticket(task_id=42, assignee_telegram_id=None)

    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=ticket)

    telegram_notify = AsyncMock(spec=TelegramNotifyPort)

    payload = make_message_created_payload(conversation_id=42)

    await process_webhook_event(
        payload=payload, ticket_repo=ticket_repo, telegram_notify=telegram_notify
    )

    telegram_notify.notify_agent.assert_not_called()


@pytest.mark.asyncio
async def test_message_created_no_notify_port_does_not_fail() -> None:
    """message_created без TelegramNotifyPort → не падает, тихо пропускает."""
    ticket = make_ticket(task_id=42, assignee_telegram_id=1001)

    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=ticket)

    payload = make_message_created_payload(conversation_id=42)

    # telegram_notify не передан (None по умолчанию)
    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)


@pytest.mark.asyncio
async def test_message_created_without_conversation_key() -> None:
    """message_created без ключа 'conversation' → не падает, пропускает."""
    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    telegram_notify = AsyncMock(spec=TelegramNotifyPort)

    payload = {
        "event": "message_created",
        "id": 1,
        "sender": {"type": "contact", "name": "Тест"},
        "content": "Тест",
    }

    await process_webhook_event(
        payload=payload, ticket_repo=ticket_repo, telegram_notify=telegram_notify
    )

    ticket_repo.get_by_id.assert_not_called()
    telegram_notify.notify_agent.assert_not_called()


# ---------------------------------------------------------------------------
# conversation_created: зеркало тикетов
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversation_created_creates_mirror_ticket() -> None:
    """conversation_created → создаёт новый SupportTicket в репозитории."""
    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=None)
    ticket_repo.save = AsyncMock()

    payload = make_conversation_created_payload(conversation_id=99, status="open", assignee_id=7)

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    ticket_repo.get_by_id.assert_called_once_with(99)
    ticket_repo.save.assert_called_once()
    saved: SupportTicket = ticket_repo.save.call_args[0][0]
    assert saved.task_id == 99
    assert saved.status == TicketStatus.OPEN
    assert saved.assignee_chatwoot_id == 7


@pytest.mark.asyncio
async def test_conversation_created_skips_existing_ticket() -> None:
    """conversation_created: тикет уже есть (создан через бот) → не перезаписывает."""
    existing_ticket = make_ticket(task_id=99)

    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=existing_ticket)
    ticket_repo.save = AsyncMock()

    payload = make_conversation_created_payload(conversation_id=99)

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    ticket_repo.save.assert_not_called()


@pytest.mark.asyncio
async def test_conversation_created_without_assignee() -> None:
    """conversation_created без назначенного агента → создаёт тикет с assignee_chatwoot_id=None."""
    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=None)
    ticket_repo.save = AsyncMock()

    payload = make_conversation_created_payload(conversation_id=55, assignee_id=None)

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    ticket_repo.save.assert_called_once()
    saved: SupportTicket = ticket_repo.save.call_args[0][0]
    assert saved.task_id == 55
    assert saved.assignee_chatwoot_id is None


@pytest.mark.asyncio
async def test_conversation_created_maps_status() -> None:
    """conversation_created: статус 'pending' корректно маппится."""
    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=None)
    ticket_repo.save = AsyncMock()

    payload = make_conversation_created_payload(conversation_id=77, status="pending")

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    saved: SupportTicket = ticket_repo.save.call_args[0][0]
    assert saved.status == TicketStatus.PENDING


@pytest.mark.asyncio
async def test_conversation_created_without_id_does_not_save() -> None:
    """conversation_created без 'id' → не сохраняет ничего."""
    ticket_repo = AsyncMock(spec=SupportTicketRepository)

    payload = {"event": "conversation_created", "status": "open", "meta": {}}

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    ticket_repo.save.assert_not_called()


# ---------------------------------------------------------------------------
# conversation_updated: синхронизация полей
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversation_updated_syncs_priority() -> None:
    """conversation_updated → обновляет приоритет тикета."""
    ticket = make_ticket(task_id=42, priority="medium")

    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=ticket)
    ticket_repo.save = AsyncMock()

    payload = make_conversation_updated_payload(conversation_id=42, priority="high")

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    ticket_repo.save.assert_called_once()
    saved: SupportTicket = ticket_repo.save.call_args[0][0]
    assert saved.priority == "high"


@pytest.mark.asyncio
async def test_conversation_updated_syncs_labels() -> None:
    """conversation_updated → обновляет labels тикета."""
    ticket = make_ticket(task_id=42, labels=[])

    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=ticket)
    ticket_repo.save = AsyncMock()

    payload = make_conversation_updated_payload(
        conversation_id=42, priority=None, labels=["urgent", "billing"]
    )

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    saved: SupportTicket = ticket_repo.save.call_args[0][0]
    assert saved.labels == ["urgent", "billing"]


@pytest.mark.asyncio
async def test_conversation_updated_syncs_assignee() -> None:
    """conversation_updated → обновляет assignee_chatwoot_id тикета."""
    ticket = make_ticket(task_id=42, assignee_chatwoot_id=5)

    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=ticket)
    ticket_repo.save = AsyncMock()

    payload = make_conversation_updated_payload(
        conversation_id=42, priority=None, labels=None, assignee_id=9
    )

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    saved: SupportTicket = ticket_repo.save.call_args[0][0]
    assert saved.assignee_chatwoot_id == 9


@pytest.mark.asyncio
async def test_conversation_updated_ticket_not_found() -> None:
    """conversation_updated: тикет не найден → не падает, ничего не сохраняет."""
    ticket_repo = AsyncMock(spec=SupportTicketRepository)
    ticket_repo.get_by_id = AsyncMock(return_value=None)
    ticket_repo.save = AsyncMock()

    payload = make_conversation_updated_payload(conversation_id=999)

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    ticket_repo.save.assert_not_called()


@pytest.mark.asyncio
async def test_conversation_updated_without_id_does_not_save() -> None:
    """conversation_updated без 'id' → не сохраняет ничего."""
    ticket_repo = AsyncMock(spec=SupportTicketRepository)

    payload = {"event": "conversation_updated", "priority": "high", "meta": {}}

    await process_webhook_event(payload=payload, ticket_repo=ticket_repo)

    ticket_repo.save.assert_not_called()
