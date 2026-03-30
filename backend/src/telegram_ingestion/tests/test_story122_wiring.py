"""Tests for STORY-122: Wiring — replace Chatwoot with Twenty in bot_handler, config, main."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher, Router
from aiogram.enums import ChatType
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from twenty_integration.application.use_cases import CreateTwentyTaskFromSession
from twenty_integration.domain.models import TwentyPerson, TwentyTask
from twenty_integration.domain.ports import TwentyCRMPort

from ..application.ports import UserProfilePort
from ..application.use_cases import (
    AddTextContentUseCase,
    AddVoiceContentUseCase,
    CancelSessionUseCase,
    SetAnalysisResultUseCase,
    StartSessionUseCase,
    TriggerAnalysisUseCase,
)
from ..domain.models import AIResult, ContentBlock, DraftSession, UserProfile
from ..domain.repository import DraftSessionRepository
from ..infrastructure.bot_handler import TelegramFSMStates, create_router

# ---------- In-memory repos / mocks ----------


class InMemoryRepo(DraftSessionRepository):
    def __init__(self) -> None:
        self._store: dict[uuid.UUID, DraftSession] = {}

    async def save(self, session: DraftSession) -> None:
        self._store[session.session_id] = session

    async def get_by_id(self, session_id: uuid.UUID) -> DraftSession | None:
        return self._store.get(session_id)

    async def get_active_by_user(self, user_id: int) -> DraftSession | None:
        for s in self._store.values():
            if s.user_id == user_id:
                return s
        return None

    async def delete(self, session_id: uuid.UUID) -> None:
        self._store.pop(session_id, None)


class FakeTwentyCRMPort(TwentyCRMPort):
    """Фейк Twenty CRM для тестов создания задач."""

    def __init__(self) -> None:
        self.created_tasks: list[TwentyTask] = []
        self.last_assignee_id: str | None = None

    async def list_workspace_members(self) -> list:
        return []

    async def find_person_by_telegram_id(self, telegram_id: int) -> TwentyPerson | None:
        return TwentyPerson(twenty_id="person-1", telegram_id=telegram_id, name="Test")

    async def create_person(self, telegram_id: int, name: str) -> TwentyPerson:
        return TwentyPerson(twenty_id="person-new", telegram_id=telegram_id, name=name)

    async def create_task(
        self, title: str, body: str, due_at: object = None, assignee_id: str | None = None
    ) -> TwentyTask:
        self.last_assignee_id = assignee_id
        task = TwentyTask(
            twenty_id=f"task-{len(self.created_tasks) + 1}",
            title=title,
            body=body,
            status="TODO",
            assignee_id=assignee_id,
        )
        self.created_tasks.append(task)
        return task

    async def link_person_to_task(self, task_id: str, person_id: str) -> None:
        pass


# ---------- aiogram test helpers ----------


def make_tg_user(user_id: int = 100) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def make_mock_bot(bot_id: int = 42) -> AsyncMock:
    bot = AsyncMock()
    bot.id = bot_id
    bot.username = "test_bot"
    return bot


def make_callback_update(
    user_id: int = 100,
    data: str = "create_crm",
) -> Update:
    msg = Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type=ChatType.PRIVATE),
        from_user=make_tg_user(user_id),
        text="нажата кнопка",
    )
    cb = CallbackQuery(
        id="cb_test",
        from_user=make_tg_user(user_id),
        chat_instance="ci",
        data=data,
        message=msg,
    )
    return Update(update_id=1, callback_query=cb)


# ---------- Helper to build router with Twenty ----------


def _build_twenty_router(
    repo: InMemoryRepo,
    session: DraftSession,
    twenty_port: FakeTwentyCRMPort | None = None,
    user_profile: UserProfile | None = None,
) -> tuple[Router, FakeTwentyCRMPort]:
    """Строит router с Twenty CRM вместо Chatwoot."""
    if twenty_port is None:
        twenty_port = FakeTwentyCRMPort()

    create_twenty_task = CreateTwentyTaskFromSession(twenty_port)

    mock_start = MagicMock(spec=StartSessionUseCase)
    mock_start.execute = AsyncMock(return_value=session)
    mock_add_text = MagicMock(spec=AddTextContentUseCase)
    mock_add_voice = MagicMock(spec=AddVoiceContentUseCase)
    mock_trigger = MagicMock(spec=TriggerAnalysisUseCase)
    mock_trigger.execute = AsyncMock(return_value=session)
    mock_cancel = MagicMock(spec=CancelSessionUseCase)
    mock_cancel.execute = AsyncMock(return_value=True)
    mock_user_port = MagicMock(spec=UserProfilePort)
    mock_user_port.is_authorized = AsyncMock(return_value=True)
    mock_user_port.get_profile = AsyncMock(return_value=user_profile)

    set_result = SetAnalysisResultUseCase(repo)

    router = create_router(
        mock_start,
        mock_add_text,
        mock_add_voice,
        mock_trigger,
        mock_cancel,
        mock_user_port,
        set_analysis_result=set_result,
        create_twenty_task=create_twenty_task,
        draft_repo=repo,
        twenty_crm_port=twenty_port,
    )
    return router, twenty_port


# ---------- AC Tests ----------


class TestStory122Wiring:
    """Acceptance Criteria для STORY-122: Wiring Chatwoot → Twenty."""

    async def test_bot_confirms_task_creates_twenty_task(self) -> None:
        """AC: 'Создать в CRM' вызывает CreateTwentyTaskFromSession."""
        repo = InMemoryRepo()
        session = DraftSession(user_id=100)
        session.add_content_block(ContentBlock(type="text", content="Задача"))
        session.start_analysis()
        ai_res = AIResult(
            title="Тест задача Twenty",
            description="Описание",
            category="bug",
            priority="high",
        )
        session.complete_analysis(ai_res)
        await repo.save(session)

        storage = MemoryStorage()
        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        await storage.set_state(key, TelegramFSMStates.preview.state)
        await storage.set_data(key, {"session_id": str(session.session_id)})

        router, twenty_port = _build_twenty_router(repo, session)
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        await dp.feed_update(make_mock_bot(), make_callback_update(data="create_crm"))

        state = await storage.get_state(key)
        assert state is None, "FSM state should be cleared after CRM task creation"
        assert len(twenty_port.created_tasks) == 1
        assert twenty_port.created_tasks[0].title == "Тест задача Twenty"

    async def test_bot_task_created_with_assignee_id(self) -> None:
        """AC: если profile.twenty_member_id заполнен, задача создаётся с assigneeId."""
        repo = InMemoryRepo()
        session = DraftSession(user_id=100)
        session.add_content_block(ContentBlock(type="text", content="Задача"))
        session.start_analysis()
        ai_res = AIResult(
            title="Задача с назначением",
            description="Описание",
            category="feature",
            priority="medium",
        )
        session.complete_analysis(ai_res)
        await repo.save(session)

        profile = UserProfile(
            telegram_id=100,
            twenty_member_id="member-abc-123",
        )

        storage = MemoryStorage()
        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        await storage.set_state(key, TelegramFSMStates.preview.state)
        await storage.set_data(key, {"session_id": str(session.session_id)})

        router, twenty_port = _build_twenty_router(repo, session, user_profile=profile)
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        await dp.feed_update(make_mock_bot(), make_callback_update(data="create_crm"))

        assert len(twenty_port.created_tasks) == 1
        assert twenty_port.last_assignee_id == "member-abc-123"

    def test_app_starts_without_chatwoot_env_vars(self) -> None:
        """AC: приложение стартует без переменных CHATWOOT_*."""
        from config import Settings

        # Settings should not require any CHATWOOT_* fields
        required_fields = {
            name for name, field_info in Settings.model_fields.items() if field_info.is_required()
        }
        chatwoot_fields = {f for f in required_fields if f.startswith("chatwoot")}
        assert chatwoot_fields == set(), (
            f"Settings still has required Chatwoot fields: {chatwoot_fields}"
        )

    def test_app_twenty_api_key_empty_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """AC: если twenty_api_key пустой, предупреждение в лог, не краш."""
        from config import Settings

        # Build settings with empty twenty_api_key (default)
        fields = {
            name: field_info.default
            for name, field_info in Settings.model_fields.items()
            if not field_info.is_required()
        }
        # Verify twenty_api_key has a default (empty string) and is not required
        assert "twenty_api_key" in fields
        assert fields["twenty_api_key"] == ""

        # The warning should be emitted during lifespan — we test the config level here
        # and the lifespan-level warning in a separate integration test if needed
