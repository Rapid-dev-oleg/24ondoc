"""Tests for /settings bot handler FSM flow."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from unittest.mock import AsyncMock as _AsyncMock
from unittest.mock import MagicMock

from aiogram import Dispatcher
from aiogram.enums import ChatType
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User
from aiogram.types import File as _TgFile
from aiogram.types import Voice as _Voice

from telegram_ingestion.application.ports import VoiceSampleStoragePort
from telegram_ingestion.application.registration_use_cases import (
    AutoRegisterUserUseCase,
    SaveVoiceSampleUseCase,
    UpdateProfileFieldUseCase,
)
from telegram_ingestion.domain.models import UserProfile, UserRole
from telegram_ingestion.domain.repository import UserProfileRepository
from telegram_ingestion.infrastructure.bot_handler import (
    SettingsFSMStates,
    create_router,
    create_settings_router,
)

from .test_bot_fsm import (
    AuthorizedUserPort,
    InMemoryRepo,
    MockSTTPort,
    UnauthorizedUserPort,
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


class InMemoryVoiceStorage(VoiceSampleStoragePort):
    def __init__(self) -> None:
        self.saved: dict[int, tuple[bytes, str]] = {}

    async def save(self, telegram_id: int, data: bytes, ext: str) -> str:
        self.saved[telegram_id] = (data, ext)
        return f"/tmp/voice/{telegram_id}.{ext}"


def _make_profile(telegram_id: int = 100) -> UserProfile:
    return UserProfile(
        telegram_id=telegram_id,
        chatwoot_user_id=10,
        chatwoot_account_id=1,
        role=UserRole.AGENT,
        settings={"display_name": "Тест", "email": f"{telegram_id}@24ondoc.ru"},
        is_active=True,
        created_at=datetime.now(UTC),
    )


def _make_message(
    user_id: int = 100,
    text: str = "/settings",
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
# Tests: /settings command
# ---------------------------------------------------------------------------


class TestSettingsCommand:
    def _make_settings_router(
        self,
        user_id: int = 100,
        authorized: bool = True,
    ) -> tuple[Dispatcher, MemoryStorage, InMemoryUserProfileRepository]:
        storage = MemoryStorage()
        repo = InMemoryUserProfileRepository()
        user_port = AuthorizedUserPort() if authorized else UnauthorizedUserPort()
        update_profile = UpdateProfileFieldUseCase(repo)
        voice_storage = InMemoryVoiceStorage()
        save_voice = SaveVoiceSampleUseCase(repo, voice_storage)

        router = create_settings_router(update_profile, save_voice, user_port)
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        return dp, storage, repo

    async def test_cmd_settings_authorized_sets_menu_state(self) -> None:
        dp, storage, repo = self._make_settings_router(user_id=100, authorized=True)
        # Pre-populate repo so authorized works via profile check
        await repo.save(_make_profile(telegram_id=100))

        bot = make_mock_bot(bot_id=42)
        update = _make_update(_make_message(user_id=100, text="/settings"))
        await dp.feed_update(bot, update)

        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        state = await storage.get_state(key)
        assert state == SettingsFSMStates.menu.state

    async def test_cmd_settings_unauthorized_no_state(self) -> None:
        dp, storage, repo = self._make_settings_router(user_id=200, authorized=False)

        bot = make_mock_bot(bot_id=42)
        update = _make_update(_make_message(user_id=200, text="/settings"))
        await dp.feed_update(bot, update)

        key = StorageKey(bot_id=42, chat_id=200, user_id=200)
        state = await storage.get_state(key)
        assert state is None


# ---------------------------------------------------------------------------
# Tests: UpdateProfileFieldUseCase via handler
# ---------------------------------------------------------------------------


class TestUpdateProfileViaHandler:
    async def test_edit_name_handler_updates_profile(self) -> None:
        storage = MemoryStorage()
        repo = InMemoryUserProfileRepository()
        await repo.save(_make_profile(telegram_id=50))
        user_port = AuthorizedUserPort()
        update_profile = UpdateProfileFieldUseCase(repo)
        voice_storage = InMemoryVoiceStorage()
        save_voice = SaveVoiceSampleUseCase(repo, voice_storage)

        router = create_settings_router(update_profile, save_voice, user_port)
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        bot = make_mock_bot(bot_id=42)

        # Put user in edit_name state
        key = StorageKey(bot_id=42, chat_id=50, user_id=50)
        await storage.set_state(key, SettingsFSMStates.edit_name.state)

        update = _make_update(_make_message(user_id=50, text="Новое Имя"))
        await dp.feed_update(bot, update)

        saved = await repo.get_by_telegram_id(50)
        assert saved is not None
        assert saved.settings.get("display_name") == "Новое Имя"

        # Should return to menu state
        state = await storage.get_state(key)
        assert state == SettingsFSMStates.menu.state

    async def test_edit_email_handler_updates_email(self) -> None:
        storage = MemoryStorage()
        repo = InMemoryUserProfileRepository()
        await repo.save(_make_profile(telegram_id=60))
        user_port = AuthorizedUserPort()
        update_profile = UpdateProfileFieldUseCase(repo)
        voice_storage = InMemoryVoiceStorage()
        save_voice = SaveVoiceSampleUseCase(repo, voice_storage)

        router = create_settings_router(update_profile, save_voice, user_port)
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        bot = make_mock_bot(bot_id=42)

        key = StorageKey(bot_id=42, chat_id=60, user_id=60)
        await storage.set_state(key, SettingsFSMStates.edit_email.state)

        update = _make_update(_make_message(user_id=60, text="newemail@example.com"))
        await dp.feed_update(bot, update)

        saved = await repo.get_by_telegram_id(60)
        assert saved is not None
        assert saved.settings.get("email") == "newemail@example.com"

        state = await storage.get_state(key)
        assert state == SettingsFSMStates.menu.state


# ---------------------------------------------------------------------------
# Tests: Auto-register on /start
# ---------------------------------------------------------------------------


class TestAutoRegisterOnStart:
    async def test_new_user_triggers_registration(self) -> None:
        from telegram_ingestion.application.ports import AgentRegistrationPort

        class FakeAgentReg(AgentRegistrationPort):
            def __init__(self) -> None:
                self.called = False

            async def create_chatwoot_agent(self, name: str, email: str, password: str) -> int:
                self.called = True
                return 999

        repo = InMemoryUserProfileRepository()
        reg = FakeAgentReg()
        auto_register = AutoRegisterUserUseCase(repo, reg, account_id=1)

        from telegram_ingestion.application.use_cases import (
            AddTextContentUseCase,
            AddVoiceContentUseCase,
            CancelSessionUseCase,
            StartSessionUseCase,
            TriggerAnalysisUseCase,
        )
        from telegram_ingestion.infrastructure.user_profile_port import UserProfilePortAdapter

        user_port = UserProfilePortAdapter(repo)
        draft_repo = InMemoryRepo()
        start_session = StartSessionUseCase(draft_repo, user_port)
        add_text = AddTextContentUseCase(draft_repo)
        add_voice = AddVoiceContentUseCase(draft_repo, MockSTTPort())
        trigger = TriggerAnalysisUseCase(draft_repo)
        cancel = CancelSessionUseCase(draft_repo)

        router = create_router(
            start_session,
            add_text,
            add_voice,
            trigger,
            cancel,
            user_port,
            auto_register=auto_register,
        )
        storage = MemoryStorage()
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        bot = make_mock_bot(bot_id=42)

        update = _make_update(_make_message(user_id=111, text="/start"))
        await dp.feed_update(bot, update)

        assert reg.called is True
        saved = await repo.get_by_telegram_id(111)
        assert saved is not None
        assert saved.chatwoot_user_id == 999

    async def test_existing_user_no_registration(self) -> None:
        from telegram_ingestion.application.ports import AgentRegistrationPort

        class FakeAgentReg(AgentRegistrationPort):
            def __init__(self) -> None:
                self.called = False

            async def create_chatwoot_agent(self, name: str, email: str, password: str) -> int:
                self.called = True
                return 888

        repo = InMemoryUserProfileRepository()
        existing = _make_profile(telegram_id=222)
        await repo.save(existing)
        reg = FakeAgentReg()
        auto_register = AutoRegisterUserUseCase(repo, reg, account_id=1)

        from telegram_ingestion.application.use_cases import (
            AddTextContentUseCase,
            AddVoiceContentUseCase,
            CancelSessionUseCase,
            StartSessionUseCase,
            TriggerAnalysisUseCase,
        )
        from telegram_ingestion.infrastructure.user_profile_port import UserProfilePortAdapter

        user_port = UserProfilePortAdapter(repo)
        draft_repo = InMemoryRepo()
        start_session = StartSessionUseCase(draft_repo, user_port)
        add_text = AddTextContentUseCase(draft_repo)
        add_voice = AddVoiceContentUseCase(draft_repo, MockSTTPort())
        trigger = TriggerAnalysisUseCase(draft_repo)
        cancel = CancelSessionUseCase(draft_repo)

        router = create_router(
            start_session,
            add_text,
            add_voice,
            trigger,
            cancel,
            user_port,
            auto_register=auto_register,
        )
        storage = MemoryStorage()
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        bot = make_mock_bot(bot_id=42)

        update = _make_update(_make_message(user_id=222, text="/start"))
        await dp.feed_update(bot, update)

        assert reg.called is False


# ---------------------------------------------------------------------------
# Tests: voice sample handler status messages
# ---------------------------------------------------------------------------


class MockSaveVoiceUseCase:
    """Configurable stub for SaveVoiceSampleUseCase."""

    def __init__(self, saved: bool, enrolled: bool) -> None:
        self._result = (saved, enrolled)
        self.calls: list[tuple[int, bytes, str]] = []

    async def execute(self, telegram_id: int, data: bytes, ext: str) -> tuple[bool, bool]:
        self.calls.append((telegram_id, data, ext))
        return self._result


def _make_bot_for_voice(audio_bytes: bytes = b"audio") -> MagicMock:
    tg_file = _TgFile(
        file_id="f1", file_unique_id="u1", file_size=len(audio_bytes), file_path="v/f.ogg"
    )
    bot = make_mock_bot(bot_id=42)
    bot.get_file = _AsyncMock(return_value=tg_file)
    bot.download_file = _AsyncMock(return_value=io.BytesIO(audio_bytes))
    return bot


def _make_voice_update(user_id: int = 100, update_id: int = 10) -> Update:
    voice = _Voice(
        file_id="voice123",
        file_unique_id="uniq123",
        duration=3,
        mime_type="audio/ogg",
        file_size=500,
    )
    msg = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type=ChatType.PRIVATE),
        from_user=User(id=user_id, is_bot=False, first_name="Tester"),
        voice=voice,
    )
    return Update(update_id=update_id, message=msg)


class TestVoiceSampleHandlerMessages:
    def _make_dp(
        self, save_voice: MockSaveVoiceUseCase
    ) -> tuple[Dispatcher, MemoryStorage, StorageKey]:
        storage = MemoryStorage()
        repo = InMemoryUserProfileRepository()
        user_port = AuthorizedUserPort()
        update_profile = UpdateProfileFieldUseCase(repo)

        router = create_settings_router(update_profile, save_voice, user_port)  # type: ignore[arg-type]
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        return dp, storage, key

    async def test_profile_not_found_use_case_is_called(self) -> None:
        save_voice = MockSaveVoiceUseCase(saved=False, enrolled=False)
        dp, storage, key = self._make_dp(save_voice)
        await storage.set_state(key, SettingsFSMStates.voice_sample.state)

        bot = _make_bot_for_voice()
        update = _make_voice_update(user_id=100)
        await dp.feed_update(bot, update)

        assert len(save_voice.calls) == 1

    async def test_enrolled_true_transitions_to_menu(self) -> None:
        save_voice = MockSaveVoiceUseCase(saved=True, enrolled=True)
        dp, storage, key = self._make_dp(save_voice)
        await storage.set_state(key, SettingsFSMStates.voice_sample.state)

        bot = _make_bot_for_voice()
        update = _make_voice_update(user_id=100)
        await dp.feed_update(bot, update)

        assert len(save_voice.calls) == 1
        state = await storage.get_state(key)
        assert state == SettingsFSMStates.menu.state

    async def test_saved_not_enrolled_transitions_to_menu(self) -> None:
        save_voice = MockSaveVoiceUseCase(saved=True, enrolled=False)
        dp, storage, key = self._make_dp(save_voice)
        await storage.set_state(key, SettingsFSMStates.voice_sample.state)

        bot = _make_bot_for_voice()
        update = _make_voice_update(user_id=100)
        await dp.feed_update(bot, update)

        assert len(save_voice.calls) == 1
        state = await storage.get_state(key)
        assert state == SettingsFSMStates.menu.state
