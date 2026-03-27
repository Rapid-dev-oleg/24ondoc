"""Tests for Telegram Bot FSM transitions and use cases."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from aiogram import Dispatcher
from aiogram.enums import ChatType
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, Update, User

from ..application.ports import STTPort, UserProfilePort
from ..application.use_cases import (
    AddTextContentUseCase,
    AddVoiceContentUseCase,
    CancelSessionUseCase,
    SetAnalysisResultUseCase,
    StartSessionUseCase,
    TriggerAnalysisUseCase,
)
from ..domain.models import (
    AIResult,
    ContentBlock,
    DraftSession,
    SessionStatus,
)
from ..domain.repository import DraftSessionRepository
from ..infrastructure.bot_handler import TelegramFSMStates, create_router

# ---------- In-memory stubs ----------


class InMemoryRepo(DraftSessionRepository):
    def __init__(self) -> None:
        self._store: dict[uuid.UUID, DraftSession] = {}

    async def get_by_id(self, session_id: uuid.UUID) -> DraftSession | None:
        return self._store.get(session_id)

    async def get_active_by_user(self, user_id: int) -> DraftSession | None:
        for s in self._store.values():
            if s.user_id == user_id:
                return s
        return None

    async def save(self, session: DraftSession) -> None:
        self._store[session.session_id] = session

    async def delete(self, session_id: uuid.UUID) -> None:
        self._store.pop(session_id, None)


class AuthorizedUserPort(UserProfilePort):
    async def is_authorized(self, telegram_id: int) -> bool:
        return True

    async def get_profile(self, telegram_id: int) -> None:
        return None

    async def list_active_agents(self) -> list:
        return []


class UnauthorizedUserPort(UserProfilePort):
    async def is_authorized(self, telegram_id: int) -> bool:
        return False

    async def get_profile(self, telegram_id: int) -> None:
        return None

    async def list_active_agents(self) -> list:
        return []


class MockSTTPort(STTPort):
    def __init__(self, result: str = "транскрипция") -> None:
        self._result = result

    async def transcribe(self, file_bytes: bytes) -> str:
        return self._result


# ---------- aiogram test helpers ----------


def make_tg_user(user_id: int = 100) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def make_message(
    user_id: int = 100,
    text: str = "/start",
    message_id: int = 1,
) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type=ChatType.PRIVATE),
        from_user=make_tg_user(user_id),
        text=text,
    )


def make_update(message: Message, update_id: int = 1) -> Update:
    return Update(update_id=update_id, message=message)


def make_mock_bot(bot_id: int = 42) -> MagicMock:
    bot = AsyncMock()
    bot.id = bot_id
    bot.username = "test_bot"
    return bot


# ---------- Tests: StartSessionUseCase ----------


class TestStartSessionUseCase:
    async def test_creates_session_for_authorized_user(self) -> None:
        repo = InMemoryRepo()
        uc = StartSessionUseCase(repo, AuthorizedUserPort())
        session = await uc.execute(telegram_id=42)
        assert session is not None
        assert session.user_id == 42
        assert session.status == SessionStatus.COLLECTING

    async def test_returns_none_for_unauthorized_user(self) -> None:
        repo = InMemoryRepo()
        uc = StartSessionUseCase(repo, UnauthorizedUserPort())
        result = await uc.execute(telegram_id=99)
        assert result is None

    async def test_replaces_existing_session(self) -> None:
        repo = InMemoryRepo()
        uc = StartSessionUseCase(repo, AuthorizedUserPort())
        first = await uc.execute(telegram_id=5)
        assert first is not None
        first_id = first.session_id
        second = await uc.execute(telegram_id=5)
        assert second is not None
        assert second.session_id != first_id
        assert await repo.get_by_id(first_id) is None


# ---------- Tests: AddTextContentUseCase ----------


class TestAddTextContentUseCase:
    async def test_adds_text_block(self) -> None:
        repo = InMemoryRepo()
        await repo.save(DraftSession(user_id=10))
        uc = AddTextContentUseCase(repo)
        result = await uc.execute(telegram_id=10, text="Привет мир")
        assert result is not None
        assert len(result.content_blocks) == 1
        assert result.content_blocks[0].content == "Привет мир"
        assert result.content_blocks[0].type == "text"

    async def test_returns_none_without_active_session(self) -> None:
        repo = InMemoryRepo()
        uc = AddTextContentUseCase(repo)
        assert await uc.execute(telegram_id=999, text="x") is None


# ---------- Tests: AddVoiceContentUseCase ----------


class TestAddVoiceContentUseCase:
    async def test_transcribes_and_adds_voice_block(self) -> None:
        repo = InMemoryRepo()
        await repo.save(DraftSession(user_id=20))
        uc = AddVoiceContentUseCase(repo, MockSTTPort("Голосовой ввод"))
        result = await uc.execute(telegram_id=20, file_id="file123", file_bytes=b"audio")
        assert result is not None
        assert len(result.content_blocks) == 1
        block = result.content_blocks[0]
        assert block.type == "voice"
        assert block.content == "Голосовой ввод"
        assert block.file_id == "file123"

    async def test_stt_port_is_called_with_file_bytes(self) -> None:
        repo = InMemoryRepo()
        await repo.save(DraftSession(user_id=21))
        stt = MockSTTPort("результат")
        uc = AddVoiceContentUseCase(repo, stt)
        result = await uc.execute(telegram_id=21, file_id="f1", file_bytes=b"data")
        assert result is not None
        assert result.content_blocks[0].content == "результат"

    async def test_returns_none_without_active_session(self) -> None:
        repo = InMemoryRepo()
        uc = AddVoiceContentUseCase(repo, MockSTTPort())
        assert await uc.execute(telegram_id=999, file_id="f", file_bytes=b"x") is None


# ---------- Tests: TriggerAnalysisUseCase ----------


class TestTriggerAnalysisUseCase:
    async def test_transitions_to_analyzing(self) -> None:
        repo = InMemoryRepo()
        session = DraftSession(user_id=30)
        session.add_content_block(ContentBlock(type="text", content="Проблема"))
        await repo.save(session)
        uc = TriggerAnalysisUseCase(repo)
        result = await uc.execute(telegram_id=30)
        assert result is not None
        assert result.status == SessionStatus.ANALYZING
        assert result.assembled_text is not None

    async def test_returns_none_without_active_session(self) -> None:
        repo = InMemoryRepo()
        uc = TriggerAnalysisUseCase(repo)
        assert await uc.execute(telegram_id=888) is None


# ---------- Tests: CancelSessionUseCase ----------


class TestCancelSessionUseCase:
    async def test_deletes_active_session(self) -> None:
        repo = InMemoryRepo()
        await repo.save(DraftSession(user_id=40))
        uc = CancelSessionUseCase(repo)
        assert await uc.execute(telegram_id=40) is True
        assert await repo.get_active_by_user(40) is None

    async def test_returns_false_without_session(self) -> None:
        repo = InMemoryRepo()
        uc = CancelSessionUseCase(repo)
        assert await uc.execute(telegram_id=777) is False


# ---------- Tests: SetAnalysisResultUseCase ----------


class TestSetAnalysisResultUseCase:
    async def test_completes_analysis_and_moves_to_preview(self) -> None:
        repo = InMemoryRepo()
        session = DraftSession(user_id=50)
        session.add_content_block(ContentBlock(type="text", content="test"))
        session.start_analysis()
        await repo.save(session)
        uc = SetAnalysisResultUseCase(repo)
        ai_result = AIResult(title="Тест", description="Описание", category="bug", priority="high")
        result = await uc.execute(session_id=session.session_id, result=ai_result)
        assert result is not None
        assert result.status == SessionStatus.PREVIEW
        assert result.ai_result is not None
        assert result.ai_result.title == "Тест"

    async def test_returns_none_for_unknown_session(self) -> None:
        repo = InMemoryRepo()
        uc = SetAnalysisResultUseCase(repo)
        ai_result = AIResult(title="t", description="d", category="bug", priority="low")
        result = await uc.execute(session_id=uuid.uuid4(), result=ai_result)
        assert result is None


# ---------- Tests: Full FSM flow via use cases ----------


class TestFSMStateTransitions:
    async def test_full_flow_collecting_through_analyzing(self) -> None:
        repo = InMemoryRepo()
        start_uc = StartSessionUseCase(repo, AuthorizedUserPort())
        add_text_uc = AddTextContentUseCase(repo)
        add_voice_uc = AddVoiceContentUseCase(repo, MockSTTPort("Голос"))
        trigger_uc = TriggerAnalysisUseCase(repo)

        session = await start_uc.execute(telegram_id=100)
        assert session is not None
        assert session.status == SessionStatus.COLLECTING

        session = await add_text_uc.execute(100, "Проблема с авторизацией")
        assert session is not None
        assert len(session.content_blocks) == 1

        session = await add_voice_uc.execute(100, "voice_1", b"audio_data")
        assert session is not None
        assert len(session.content_blocks) == 2
        assert session.content_blocks[1].type == "voice"
        assert session.content_blocks[1].content == "Голос"

        session = await trigger_uc.execute(100)
        assert session is not None
        assert session.status == SessionStatus.ANALYZING

    async def test_analyzing_to_preview(self) -> None:
        repo = InMemoryRepo()
        session = DraftSession(user_id=200)
        session.add_content_block(ContentBlock(type="text", content="text"))
        session.start_analysis()
        await repo.save(session)

        result = await SetAnalysisResultUseCase(repo).execute(
            session.session_id,
            AIResult(title="T", description="D", category="bug", priority="low"),
        )
        assert result is not None
        assert result.status == SessionStatus.PREVIEW

    async def test_cancel_from_collecting(self) -> None:
        repo = InMemoryRepo()
        await StartSessionUseCase(repo, AuthorizedUserPort()).execute(telegram_id=300)
        cancelled = await CancelSessionUseCase(repo).execute(300)
        assert cancelled is True
        assert await repo.get_active_by_user(300) is None

    async def test_new_task_replaces_previous_session(self) -> None:
        repo = InMemoryRepo()
        start_uc = StartSessionUseCase(repo, AuthorizedUserPort())
        s1 = await start_uc.execute(400)
        assert s1 is not None
        s2 = await start_uc.execute(400)
        assert s2 is not None
        assert s1.session_id != s2.session_id
        assert await repo.get_by_id(s1.session_id) is None


# ---------- Tests: Bot Handler via aiogram Dispatcher ----------


class TestBotHandlers:
    """Handler tests using aiogram Dispatcher with MemoryStorage."""

    def _make_use_cases(
        self,
        authorized: bool = True,
        session: DraftSession | None = None,
    ) -> tuple[
        MagicMock,
        MagicMock,
        MagicMock,
        MagicMock,
        MagicMock,
    ]:
        if session is None:
            session = DraftSession(user_id=100) if authorized else None

        mock_start = MagicMock(spec=StartSessionUseCase)
        mock_start.execute = AsyncMock(return_value=session)

        mock_add_text = MagicMock(spec=AddTextContentUseCase)
        mock_add_text.execute = AsyncMock(return_value=session)

        mock_add_voice = MagicMock(spec=AddVoiceContentUseCase)
        mock_add_voice.execute = AsyncMock(return_value=session)

        mock_trigger = MagicMock(spec=TriggerAnalysisUseCase)
        mock_trigger.execute = AsyncMock(return_value=session)

        mock_cancel = MagicMock(spec=CancelSessionUseCase)
        mock_cancel.execute = AsyncMock(return_value=True)

        return mock_start, mock_add_text, mock_add_voice, mock_trigger, mock_cancel

    async def test_cmd_new_task_authorized_sets_collecting_state(self) -> None:
        storage = MemoryStorage()
        mock_start, mock_add_text, mock_add_voice, mock_trigger, mock_cancel = self._make_use_cases(
            authorized=True
        )
        router = create_router(mock_start, mock_add_text, mock_add_voice, mock_trigger, mock_cancel)
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        bot = make_mock_bot(bot_id=42)

        update = make_update(make_message(user_id=100, text="/new_task"))
        await dp.feed_update(bot, update)

        mock_start.execute.assert_called_once_with(100)
        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        state = await storage.get_state(key)
        assert state == TelegramFSMStates.collecting.state

    async def test_cmd_new_task_unauthorized_no_state_change(self) -> None:
        storage = MemoryStorage()
        mock_start, mock_add_text, mock_add_voice, mock_trigger, mock_cancel = self._make_use_cases(
            authorized=False, session=None
        )
        router = create_router(mock_start, mock_add_text, mock_add_voice, mock_trigger, mock_cancel)
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        bot = make_mock_bot(bot_id=42)

        update = make_update(make_message(user_id=100, text="/new_task"))
        await dp.feed_update(bot, update)

        mock_start.execute.assert_called_once_with(100)
        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        state = await storage.get_state(key)
        assert state is None

    async def test_cmd_start_clears_state(self) -> None:
        storage = MemoryStorage()
        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        await storage.set_state(key, TelegramFSMStates.collecting.state)

        mock_start, mock_add_text, mock_add_voice, mock_trigger, mock_cancel = (
            self._make_use_cases()
        )
        router = create_router(mock_start, mock_add_text, mock_add_voice, mock_trigger, mock_cancel)
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        bot = make_mock_bot(bot_id=42)

        update = make_update(make_message(user_id=100, text="/start"))
        await dp.feed_update(bot, update)

        state = await storage.get_state(key)
        assert state is None

    async def test_text_in_collecting_calls_add_text(self) -> None:
        storage = MemoryStorage()
        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        await storage.set_state(key, TelegramFSMStates.collecting.state)

        mock_start, mock_add_text, mock_add_voice, mock_trigger, mock_cancel = (
            self._make_use_cases()
        )
        router = create_router(mock_start, mock_add_text, mock_add_voice, mock_trigger, mock_cancel)
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        bot = make_mock_bot(bot_id=42)

        update = make_update(make_message(user_id=100, text="Проблема с кнопкой"))
        await dp.feed_update(bot, update)

        mock_add_text.execute.assert_called_once_with(100, "Проблема с кнопкой")
