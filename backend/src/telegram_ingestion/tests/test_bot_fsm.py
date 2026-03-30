"""Tests for Telegram Bot FSM transitions and use cases."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from aiogram import Dispatcher, Router
from aiogram.enums import ChatType
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from ai_classification.domain.models import (
    Category,
    ClassificationResult,
    Priority,
)
from ai_classification.domain.repository import AIClassificationPort
from twenty_integration.application.use_cases import CreateTwentyTaskFromSession
from twenty_integration.domain.models import TwentyMember, TwentyPerson, TwentyTask
from twenty_integration.domain.ports import TwentyCRMPort

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
    UserProfile,
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

    async def get_profile(self, telegram_id: int) -> UserProfile | None:
        return None

    async def list_active_agents(self) -> list[UserProfile]:
        return []

    async def update_twenty_member_id(
        self, telegram_id: int, twenty_member_id: str
    ) -> UserProfile | None:
        return None


class UnauthorizedUserPort(UserProfilePort):
    async def is_authorized(self, telegram_id: int) -> bool:
        return False

    async def get_profile(self, telegram_id: int) -> UserProfile | None:
        return None

    async def list_active_agents(self) -> list[UserProfile]:
        return []

    async def update_twenty_member_id(
        self, telegram_id: int, twenty_member_id: str
    ) -> UserProfile | None:
        return None


class MockSTTPort(STTPort):
    def __init__(self, result: str = "транскрипция") -> None:
        self._result = result

    async def transcribe(self, file_bytes: bytes) -> str:
        return self._result


class MockAIPort(AIClassificationPort):
    def __init__(self, raise_error: bool = False) -> None:
        self._raise_error = raise_error

    async def classify(self, text: str) -> ClassificationResult:
        if self._raise_error:
            raise RuntimeError("AI service unavailable")
        return ClassificationResult(
            source_text=text,
            title="Проблема с кнопкой",
            description="Подробное описание",
            category=Category.BUG,
            priority=Priority.HIGH,
        )


class InMemoryTwentyForCreate(TwentyCRMPort):
    def __init__(self) -> None:
        self.created: list[TwentyTask] = []

    async def list_workspace_members(self) -> list[TwentyMember]:
        return []

    async def find_person_by_telegram_id(self, telegram_id: int) -> TwentyPerson | None:
        return TwentyPerson(twenty_id="person-1", telegram_id=telegram_id, name="Test")

    async def create_person(self, telegram_id: int, name: str) -> TwentyPerson:
        return TwentyPerson(twenty_id="person-new", telegram_id=telegram_id, name=name)

    async def create_task(
        self, title: str, body: str, due_at: object = None, assignee_id: str | None = None
    ) -> TwentyTask:
        task = TwentyTask(
            twenty_id=f"task-{len(self.created) + 1}",
            title=title,
            body=body,
            status="TODO",
            assignee_id=assignee_id,
        )
        self.created.append(task)
        return task

    async def link_person_to_task(self, task_id: str, person_id: str) -> None:
        pass


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


def make_callback(
    user_id: int = 100,
    data: str = "collect",
    message_id: int = 10,
) -> CallbackQuery:
    msg = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type=ChatType.PRIVATE),
        from_user=make_tg_user(user_id),
        text="нажата кнопка",
    )
    return CallbackQuery(
        id="cb_test",
        from_user=make_tg_user(user_id),
        chat_instance="ci",
        data=data,
        message=msg,
    )


def make_callback_update(
    user_id: int = 100,
    data: str = "collect",
    update_id: int = 2,
) -> Update:
    return Update(update_id=update_id, callback_query=make_callback(user_id=user_id, data=data))


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
        user_port_authorized: bool = True,
    ) -> tuple[
        MagicMock,
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

        mock_user_port = MagicMock(spec=UserProfilePort)
        mock_user_port.is_authorized = AsyncMock(return_value=user_port_authorized)
        mock_user_port.get_profile = AsyncMock(return_value=None)
        mock_user_port.list_active_agents = AsyncMock(return_value=[])

        return (
            mock_start,
            mock_add_text,
            mock_add_voice,
            mock_trigger,
            mock_cancel,
            mock_user_port,
        )

    async def test_cmd_new_task_authorized_sets_collecting_state(self) -> None:
        storage = MemoryStorage()
        (
            mock_start,
            mock_add_text,
            mock_add_voice,
            mock_trigger,
            mock_cancel,
            mock_user_port,
        ) = self._make_use_cases(authorized=True)
        router = create_router(
            mock_start,
            mock_add_text,
            mock_add_voice,
            mock_trigger,
            mock_cancel,
            mock_user_port,
        )
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
        (
            mock_start,
            mock_add_text,
            mock_add_voice,
            mock_trigger,
            mock_cancel,
            mock_user_port,
        ) = self._make_use_cases(authorized=False, session=None)
        router = create_router(
            mock_start,
            mock_add_text,
            mock_add_voice,
            mock_trigger,
            mock_cancel,
            mock_user_port,
        )
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

        (
            mock_start,
            mock_add_text,
            mock_add_voice,
            mock_trigger,
            mock_cancel,
            mock_user_port,
        ) = self._make_use_cases()
        router = create_router(
            mock_start,
            mock_add_text,
            mock_add_voice,
            mock_trigger,
            mock_cancel,
            mock_user_port,
        )
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

        (
            mock_start,
            mock_add_text,
            mock_add_voice,
            mock_trigger,
            mock_cancel,
            mock_user_port,
        ) = self._make_use_cases()
        router = create_router(
            mock_start,
            mock_add_text,
            mock_add_voice,
            mock_trigger,
            mock_cancel,
            mock_user_port,
        )
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        bot = make_mock_bot(bot_id=42)

        update = make_update(make_message(user_id=100, text="Проблема с кнопкой"))
        await dp.feed_update(bot, update)

        mock_add_text.execute.assert_called_once_with(100, "Проблема с кнопкой")

    async def test_cmd_start_authorized_sends_welcome(self) -> None:
        storage = MemoryStorage()
        (
            mock_start,
            mock_add_text,
            mock_add_voice,
            mock_trigger,
            mock_cancel,
            mock_user_port,
        ) = self._make_use_cases(user_port_authorized=True)
        router = create_router(
            mock_start,
            mock_add_text,
            mock_add_voice,
            mock_trigger,
            mock_cancel,
            mock_user_port,
        )
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        bot = make_mock_bot(bot_id=42)

        update = make_update(make_message(user_id=100, text="/start"))
        await dp.feed_update(bot, update)

        # State should be None (cleared, no phone flow)
        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        state = await storage.get_state(key)
        assert state is None

    async def test_cmd_start_unauthorized_no_state_change(self) -> None:
        """Unauthorized users get 'contact admin' message, no FSM state set."""
        storage = MemoryStorage()
        (
            mock_start,
            mock_add_text,
            mock_add_voice,
            mock_trigger,
            mock_cancel,
            mock_user_port,
        ) = self._make_use_cases(user_port_authorized=False)
        router = create_router(
            mock_start,
            mock_add_text,
            mock_add_voice,
            mock_trigger,
            mock_cancel,
            mock_user_port,
        )
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        bot = make_mock_bot(bot_id=42)

        update = make_update(make_message(user_id=100, text="/start"))
        await dp.feed_update(bot, update)

        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        state = await storage.get_state(key)
        # No awaiting_phone state — just stay at None
        assert state is None


# ---------- Tests: Preview Flow (callback_collect → analyzing → preview) ----------


class TestPreviewFlow:
    """Тесты перехода collecting → preview через AI-анализ."""

    def _build_router(
        self,
        repo: InMemoryRepo,
        session: DraftSession,
        ai_raise_error: bool = False,
    ) -> tuple[Router, InMemoryTwentyForCreate]:
        """Строит router с реальными use cases и mock-ботом."""
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
        mock_user_port.get_profile = AsyncMock(return_value=None)

        ai_port = MockAIPort(raise_error=ai_raise_error)
        set_result = SetAnalysisResultUseCase(repo)
        twenty = InMemoryTwentyForCreate()
        create_twenty_task = CreateTwentyTaskFromSession(twenty)

        router = create_router(
            mock_start,
            mock_add_text,
            mock_add_voice,
            mock_trigger,
            mock_cancel,
            mock_user_port,
            ai_port=ai_port,
            set_analysis_result=set_result,
            create_twenty_task=create_twenty_task,
            draft_repo=repo,
        )
        return router, twenty

    async def test_callback_collect_transitions_to_preview_on_success(self) -> None:
        """При успешном AI-анализе состояние должно стать preview."""
        repo = InMemoryRepo()
        session = DraftSession(user_id=100)
        session.add_content_block(ContentBlock(type="text", content="Тест проблема"))
        session.start_analysis()
        await repo.save(session)

        storage = MemoryStorage()
        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        await storage.set_state(key, TelegramFSMStates.collecting.state)
        await storage.set_data(key, {"session_id": str(session.session_id)})

        router, _ = self._build_router(repo, session)
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        bot = make_mock_bot(bot_id=42)

        await dp.feed_update(bot, make_callback_update(user_id=100, data="collect"))

        state = await storage.get_state(key)
        assert state == TelegramFSMStates.preview.state

    async def test_callback_collect_stores_ai_result_in_session(self) -> None:
        """После успешного анализа ai_result должен быть сохранён в сессии."""
        repo = InMemoryRepo()
        session = DraftSession(user_id=100)
        session.add_content_block(ContentBlock(type="text", content="Тест"))
        session.start_analysis()
        await repo.save(session)

        storage = MemoryStorage()
        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        await storage.set_state(key, TelegramFSMStates.collecting.state)
        await storage.set_data(key, {"session_id": str(session.session_id)})

        router, _ = self._build_router(repo, session)
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        await dp.feed_update(make_mock_bot(), make_callback_update(data="collect"))

        updated = await repo.get_by_id(session.session_id)
        assert updated is not None
        assert updated.ai_result is not None
        assert updated.ai_result.title == "Проблема с кнопкой"
        assert updated.status == SessionStatus.PREVIEW

    async def test_callback_collect_reverts_to_collecting_on_ai_error(self) -> None:
        """При ошибке AI состояние должно вернуться в collecting."""
        repo = InMemoryRepo()
        session = DraftSession(user_id=100)
        session.add_content_block(ContentBlock(type="text", content="Тест"))
        session.start_analysis()
        await repo.save(session)

        storage = MemoryStorage()
        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        await storage.set_state(key, TelegramFSMStates.collecting.state)
        await storage.set_data(key, {"session_id": str(session.session_id)})

        router, _ = self._build_router(repo, session, ai_raise_error=True)
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        await dp.feed_update(make_mock_bot(), make_callback_update(data="collect"))

        state = await storage.get_state(key)
        assert state == TelegramFSMStates.collecting.state

    async def test_callback_create_crm_creates_ticket_and_clears_state(self) -> None:
        """Нажатие create_crm должно создать тикет и очистить FSM состояние."""
        repo = InMemoryRepo()
        session = DraftSession(user_id=100)
        session.add_content_block(ContentBlock(type="text", content="Задача"))
        session.start_analysis()
        ai_res = AIResult(
            title="Тест задача",
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

        router, twenty = self._build_router(repo, session)
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        await dp.feed_update(make_mock_bot(), make_callback_update(data="create_crm"))

        state = await storage.get_state(key)
        assert state is None
        assert len(twenty.created) == 1
        assert twenty.created[0].title == "Тест задача"

    async def test_reanalyze_callback_transitions_to_preview(self) -> None:
        """Повторный анализ из preview тоже должен завершаться в preview."""
        repo = InMemoryRepo()
        session = DraftSession(user_id=100)
        session.add_content_block(ContentBlock(type="text", content="Данные"))
        session.start_analysis()
        ai_res = AIResult(
            title="Старый заголовок",
            description="Старое описание",
            category="bug",
            priority="medium",
        )
        session.complete_analysis(ai_res)
        # Re-start analysis for reanalyze flow
        session.start_editing()
        session.start_analysis()
        await repo.save(session)

        storage = MemoryStorage()
        key = StorageKey(bot_id=42, chat_id=100, user_id=100)
        await storage.set_state(key, TelegramFSMStates.preview.state)
        await storage.set_data(key, {"session_id": str(session.session_id)})

        router, _ = self._build_router(repo, session)
        dp = Dispatcher(storage=storage)
        dp.include_router(router)
        await dp.feed_update(make_mock_bot(), make_callback_update(data="reanalyze"))

        state = await storage.get_state(key)
        assert state == TelegramFSMStates.preview.state
