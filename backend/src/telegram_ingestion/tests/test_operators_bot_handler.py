"""Tests for /operators bot handler FSM flow (DEV-122)."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Dispatcher
from aiogram.enums import ChatType
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User

from telegram_ingestion.application.ports import UserProfilePort
from telegram_ingestion.application.use_cases import (
    AddTextContentUseCase,
    AddVoiceContentUseCase,
    CancelSessionUseCase,
    StartSessionUseCase,
    TriggerAnalysisUseCase,
)
from telegram_ingestion.domain.models import UserProfile, UserRole
from telegram_ingestion.domain.repository import UserProfileRepository
from telegram_ingestion.infrastructure.bot_handler import (
    OperatorLinkStates,
    create_router,
)
from twenty_integration.domain.models import TwentyMember
from twenty_integration.domain.ports import TwentyCRMPort

from .test_bot_fsm import (
    InMemoryRepo,
    MockSTTPort,
    make_mock_bot,
)

# ---------------------------------------------------------------------------
# In-memory stubs
# ---------------------------------------------------------------------------


class InMemoryUserProfileRepository(UserProfileRepository):
    def __init__(self) -> None:
        self._store: dict[int, UserProfile] = {}

    async def get_by_telegram_id(self, telegram_id: int) -> UserProfile | None:
        return self._store.get(telegram_id)

    async def get_by_chatwoot_id(self, chatwoot_user_id: int) -> UserProfile | None:
        for p in self._store.values():
            if p.chatwoot_user_id == chatwoot_user_id:
                return p
        return None

    async def save(self, profile: UserProfile) -> None:
        self._store[profile.telegram_id] = profile

    async def list_active(self) -> list[UserProfile]:
        return [p for p in self._store.values() if p.is_active]

    async def list_all(self) -> list[UserProfile]:
        return list(self._store.values())

    async def delete_by_telegram_id(self, telegram_id: int) -> None:
        self._store.pop(telegram_id, None)


class UserProfilePortAdapter(UserProfilePort):
    def __init__(self, repo: UserProfileRepository) -> None:
        self._repo = repo

    async def is_authorized(self, telegram_id: int) -> bool:
        profile = await self._repo.get_by_telegram_id(telegram_id)
        return profile is not None

    async def get_profile(self, telegram_id: int) -> UserProfile | None:
        return await self._repo.get_by_telegram_id(telegram_id)

    async def list_active_agents(self) -> list[UserProfile]:
        return await self._repo.list_active()

    async def update_twenty_member_id(
        self, telegram_id: int, twenty_member_id: str
    ) -> UserProfile | None:
        profile = await self._repo.get_by_telegram_id(telegram_id)
        if profile is None:
            return None
        profile.twenty_member_id = twenty_member_id
        await self._repo.save(profile)
        return profile


class MockTwentyCRMPort(TwentyCRMPort):
    def __init__(self) -> None:
        self.members = [
            TwentyMember(
                twenty_id="member-1",
                first_name="John",
                last_name="Doe",
                email="john@example.com",
            ),
            TwentyMember(
                twenty_id="member-2",
                first_name="Jane",
                last_name="Smith",
                email="jane@example.com",
            ),
        ]

    async def list_workspace_members(self) -> list[TwentyMember]:
        return self.members

    async def find_person_by_telegram_id(self, telegram_id: int):
        return None

    async def create_person(self, telegram_id: int, name: str):
        pass

    async def create_task(self, title: str, body: str, due_at=None, assignee_id=None):
        pass

    async def link_person_to_task(self, task_id: str, person_id: str) -> None:
        pass


def _make_profile(telegram_id: int = 100, role: UserRole = UserRole.ADMIN) -> UserProfile:
    return UserProfile(
        telegram_id=telegram_id,
        chatwoot_user_id=10 + telegram_id,
        chatwoot_account_id=1,
        role=role,
        settings={"display_name": "Тест", "email": f"{telegram_id}@24ondoc.ru"},
        is_active=True,
        created_at=datetime.now(UTC),
    )


def _make_message(
    user_id: int = 100,
    text: str = "/operators",
    message_id: int = 1,
) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type=ChatType.PRIVATE),
        from_user=User(id=user_id, is_bot=False, first_name="Test"),
        text=text,
    )


def _make_update(message: Message, update_id: int = 1) -> Update:
    return Update(update_id=update_id, message=message)


# ---------------------------------------------------------------------------
# Tests: /operators command
# ---------------------------------------------------------------------------


class TestOperatorsCommand:
    def _make_router(
        self,
        user_id: int = 100,
        authorized: bool = True,
        admin: bool = True,
        twenty_crm_available: bool = True,
    ) -> tuple[Dispatcher, MemoryStorage, InMemoryUserProfileRepository]:
        storage = MemoryStorage()
        repo = InMemoryUserProfileRepository()
        user_port = UserProfilePortAdapter(repo)

        # Create use cases
        draft_repo = InMemoryRepo()
        start_session = StartSessionUseCase(draft_repo, user_port)
        add_text = AddTextContentUseCase(draft_repo)
        stt_port = MockSTTPort()
        add_voice = AddVoiceContentUseCase(draft_repo, stt_port)
        trigger_analysis = TriggerAnalysisUseCase(draft_repo)
        cancel_session = CancelSessionUseCase(draft_repo)

        twenty_crm_port = MockTwentyCRMPort() if twenty_crm_available else None

        router = create_router(
            start_session,
            add_text,
            add_voice,
            trigger_analysis,
            cancel_session,
            user_port,
            twenty_crm_port=twenty_crm_port,
        )
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        return dp, storage, repo

    async def test_admin_lists_workspace_members(self) -> None:
        """Admin получает список членов workspace при вызове /operators."""
        dp, storage, repo = self._make_router(user_id=100, admin=True, authorized=True)
        await repo.save(_make_profile(telegram_id=100, role=UserRole.ADMIN))

        bot = make_mock_bot(bot_id=42)
        update = _make_update(_make_message(user_id=100, text="/operators"))
        await dp.feed_update(bot, update)

        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        state = await storage.get_state(key)
        assert state == OperatorLinkStates.choosing_member.state

    async def test_non_admin_cannot_use_operators_command(self) -> None:
        """Обычный пользователь не может использовать /operators."""
        dp, storage, repo = self._make_router(user_id=200, admin=False, authorized=True)
        await repo.save(_make_profile(telegram_id=200, role=UserRole.AGENT))

        bot = make_mock_bot(bot_id=42)
        update = _make_update(_make_message(user_id=200, text="/operators"))
        await dp.feed_update(bot, update)

        key = StorageKey(bot_id=42, chat_id=200, user_id=200)
        state = await storage.get_state(key)
        # State should not be changed
        assert state is None

    async def test_admin_links_member_to_telegram_id(self) -> None:
        """После выбора члена и ввода telegram_id, сохраняется twenty_member_id."""
        dp, storage, repo = self._make_router(user_id=100, admin=True)
        admin_profile = _make_profile(telegram_id=100, role=UserRole.ADMIN)
        await repo.save(admin_profile)

        # Create operator profile that we'll link to
        operator_profile = _make_profile(telegram_id=200, role=UserRole.AGENT)
        await repo.save(operator_profile)

        bot = make_mock_bot(bot_id=42)

        # Step 1: /operators command
        update = _make_update(_make_message(user_id=100, text="/operators", message_id=1))
        await dp.feed_update(bot, update)

        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        state = await storage.get_state(key)
        assert state == OperatorLinkStates.choosing_member.state

        # Step 2: Select member via callback (simulated by direct message in state)
        # In real flow, this would be callback_query, but for simplicity we'll test message flow
        await storage.set_state(key, OperatorLinkStates.choosing_member.state)
        await storage.set_data(key, {"twenty_member_id": "member-1"})
        await storage.set_state(key, OperatorLinkStates.entering_telegram_id.state)

        # Step 3: Send telegram_id
        update = _make_update(_make_message(user_id=100, text="200", message_id=2))
        await dp.feed_update(bot, update)

        # Verify twenty_member_id was updated
        updated_profile = await repo.get_by_telegram_id(200)
        assert updated_profile is not None
        assert updated_profile.twenty_member_id == "member-1"

    async def test_supervisor_can_use_operators_command(self) -> None:
        """Supervisor может использовать /operators также как admin."""
        dp, storage, repo = self._make_router(user_id=300, admin=False)
        supervisor_profile = _make_profile(telegram_id=300, role=UserRole.SUPERVISOR)
        await repo.save(supervisor_profile)

        bot = make_mock_bot(bot_id=42)
        update = _make_update(_make_message(user_id=300, text="/operators"))
        await dp.feed_update(bot, update)

        key = StorageKey(bot_id=42, chat_id=300, user_id=300)
        state = await storage.get_state(key)
        assert state == OperatorLinkStates.choosing_member.state
