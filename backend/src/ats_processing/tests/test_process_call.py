"""Tests for ProcessCallWebhook use case and Telegram notifications (DEV-53)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from aiogram import Dispatcher
from aiogram.enums import ChatType
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from ..application.ports import VoiceEmbeddingPort
from ..application.use_cases import (
    IdentifyAgentByVoice,
    ProcessCallWebhook,
    TelegramNotificationPort,
)
from ..domain.models import CallRecord, CallStatus
from ..domain.repository import AgentVoiceSampleRepository, CallRecordRepository

# ---------- Stubs ----------


class StubCallRepo(CallRecordRepository):
    def __init__(self, record: CallRecord | None = None) -> None:
        self._record = record
        self.saved: list[CallRecord] = []

    async def get_by_id(self, call_id: str) -> CallRecord | None:
        return self._record

    async def save(self, record: CallRecord) -> None:
        self.saved.append(record)
        self._record = record

    async def get_pending(self, limit: int = 10, source: object | None = None) -> list[CallRecord]:
        return []

    async def find_recent_by_phone(self, phone: str, limit: int = 10) -> list[CallRecord]:
        return []


class StubEmbeddingPort(VoiceEmbeddingPort):
    async def embed(self, audio_bytes: bytes) -> list[float]:
        return [0.1] * 384


class StubVoiceRepo(AgentVoiceSampleRepository):
    def __init__(self, closest: tuple[int, float] | None = None) -> None:
        self._closest = closest

    async def find_closest(self, embedding: list[float]) -> tuple[int, float] | None:
        return self._closest

    async def save(self, agent_id: int, embedding: list[float]) -> None:
        pass


class StubNotificationPort(TelegramNotificationPort):
    def __init__(self) -> None:
        self.sent: list[tuple[int, CallRecord]] = []

    async def send_call_notification(self, chat_id: int, call_record: CallRecord) -> None:
        self.sent.append((chat_id, call_record))


def _make_call(call_id: str = "t2_001") -> CallRecord:
    return CallRecord(
        call_id=call_id,
        audio_url="https://t2.example.com/rec/001.mp3",
        caller_phone="+79991234567",
    )


def _build_process_call_webhook(
    call_record: CallRecord | None = None,
    identify_raises: Exception | None = None,
    dispatcher_chat_id: int = 999,
) -> tuple[ProcessCallWebhook, StubCallRepo, StubNotificationPort]:
    repo = StubCallRepo(call_record)
    notification = StubNotificationPort()

    embedding_port = StubEmbeddingPort()
    voice_repo = StubVoiceRepo()

    identify_agent = IdentifyAgentByVoice(embedding_port, voice_repo)

    if identify_raises:

        async def patched_execute(cr: CallRecord, audio_bytes: bytes) -> int | None:
            raise identify_raises

        identify_agent.execute = patched_execute  # type: ignore[assignment]

    uc = ProcessCallWebhook(
        call_repo=repo,
        identify_agent=identify_agent,
        notification_port=notification,
        dispatcher_chat_id=dispatcher_chat_id,
    )
    return uc, repo, notification


# ============================================================
# Tests: ProcessCallWebhook
# ============================================================


class TestProcessCallWebhook:
    async def test_process_call_webhook_full_flow(self) -> None:
        """AC: полный flow без ошибок, статус PREVIEW."""
        call = _make_call("t2_full")
        uc, repo, notification = _build_process_call_webhook(call_record=call)

        result = await uc.execute("t2_full")

        assert result is not None
        assert result.status == CallStatus.PREVIEW
        assert len(notification.sent) == 1
        assert notification.sent[0][0] == 999

    async def test_process_call_webhook_handles_identify_error(self) -> None:
        """AC: ошибка identify → статус ERROR."""
        call = _make_call("t2_err")
        uc, repo, notification = _build_process_call_webhook(
            call_record=call,
            identify_raises=RuntimeError("Voice processing error"),
        )

        result = await uc.execute("t2_err")

        assert result is not None
        assert result.status == CallStatus.ERROR
        assert len(notification.sent) == 0

    async def test_process_call_webhook_not_found_returns_none(self) -> None:
        """CallRecord не найден → None."""
        uc, _, _ = _build_process_call_webhook(call_record=None)
        result = await uc.execute("nonexistent")
        assert result is None

    async def test_telegram_notification_sent_with_inline_buttons(self) -> None:
        """AC: уведомление с кнопками (notification_port.send called)."""
        call = _make_call("t2_notif")
        uc, repo, notification = _build_process_call_webhook(
            call_record=call, dispatcher_chat_id=12345
        )

        await uc.execute("t2_notif")

        assert len(notification.sent) == 1
        assert notification.sent[0][0] == 12345
        assert notification.sent[0][1].call_id == "t2_notif"


# ============================================================
# Tests: Telegram callback handlers
# ============================================================


def _make_tg_user(user_id: int = 100) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def _make_callback(
    user_id: int = 100, data: str = "call_action:t2_001:ignore", message_id: int = 1
) -> CallbackQuery:
    msg = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type=ChatType.PRIVATE),
        from_user=_make_tg_user(user_id),
        text="звонок",
    )
    return CallbackQuery(
        id="cb1",
        from_user=_make_tg_user(user_id),
        chat_instance="ci",
        data=data,
        message=msg,
    )


def _build_call_router_dp(
    call_record: CallRecord | None = None,
    chatwoot_port: object | None = None,
) -> tuple[Dispatcher, StubCallRepo, MemoryStorage]:
    from telegram_ingestion.infrastructure.bot_handler import (
        ChatwootPortLike,
        create_call_notification_router,
    )

    repo = StubCallRepo(call_record)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    cw_port: ChatwootPortLike | None = chatwoot_port  # type: ignore[assignment]
    router = create_call_notification_router(call_repo=repo, chatwoot_port=cw_port)
    dp.include_router(router)
    return dp, repo, storage


class TestCallNotificationRouter:
    async def test_telegram_ignore_action_closes_record(self) -> None:
        """AC: нажатие Игнорировать → mark_error + save."""
        call = _make_call("t2_ign")
        dp, repo, _ = _build_call_router_dp(call_record=call)
        bot = AsyncMock()
        bot.id = 42
        bot.username = "test_bot"

        update = Update(
            update_id=1,
            callback_query=_make_callback(data="call_action:t2_ign:ignore"),
        )
        await dp.feed_update(bot, update)

        assert len(repo.saved) == 1
        assert repo.saved[0].status == CallStatus.ERROR

    async def test_telegram_create_action_triggers_chatwoot(self) -> None:
        """AC: нажатие Создать тикет → chatwoot_port.create_ticket_from_call."""
        call = _make_call("t2_crt")
        chatwoot_port = AsyncMock()
        chatwoot_port.create_ticket_from_call = AsyncMock()
        dp, repo, _ = _build_call_router_dp(call_record=call, chatwoot_port=chatwoot_port)
        bot = AsyncMock()
        bot.id = 42
        bot.username = "test_bot"

        update = Update(
            update_id=1,
            callback_query=_make_callback(data="call_action:t2_crt:create"),
        )
        await dp.feed_update(bot, update)

        chatwoot_port.create_ticket_from_call.assert_awaited_once_with("t2_crt")

    async def test_telegram_edit_action_returns_message(self) -> None:
        """✏️ Изменить — не крашится."""
        call = _make_call("t2_edt")
        dp, _, _ = _build_call_router_dp(call_record=call)
        bot = AsyncMock()
        bot.id = 42
        bot.username = "test_bot"

        update = Update(
            update_id=1,
            callback_query=_make_callback(data="call_action:t2_edt:edit"),
        )
        await dp.feed_update(bot, update)  # должен завершиться без исключений

    async def test_telegram_ignore_no_record_does_not_crash(self) -> None:
        """Ignore при отсутствующей записи — не падает."""
        dp, _, _ = _build_call_router_dp(call_record=None)
        bot = AsyncMock()
        bot.id = 42
        bot.username = "test_bot"

        update = Update(
            update_id=1,
            callback_query=_make_callback(data="call_action:missing:ignore"),
        )
        await dp.feed_update(bot, update)
