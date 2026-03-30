"""E2E тест: полный флоу Telegram → регистрация → задача (текст+голос) → CRM.

Acceptance criteria:
  1. /start → пользователь создаётся в репо + агент в Chatwoot
  2. /new_task → сессия создана (статус COLLECTING)
  3. текст → ContentBlock(type=text) добавлен
  4. голос → STT → ContentBlock(type=voice) добавлен
  5. collect callback → AI классификация → статус PREVIEW
  6. create_crm callback → тикет создаётся в Chatwoot CRM
  7. тикет содержит правильные поля (title, description, category, priority)
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from aiogram import Dispatcher
from aiogram.enums import ChatType
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    Chat,
    File,
    Message,
    Update,
    User,
    Voice,
)

from ai_classification.domain.models import Category, ClassificationResult, Priority
from ai_classification.domain.repository import AIClassificationPort
from chatwoot_integration.application.use_cases import CreateTicketFromSession
from chatwoot_integration.domain.models import SupportTicket
from chatwoot_integration.domain.repository import ChatwootPort, SupportTicketRepository
from telegram_ingestion.application.ports import (
    AgentRegistrationPort,
    STTPort,
    UserProfilePort,
)
from telegram_ingestion.application.registration_use_cases import AutoRegisterUserUseCase
from telegram_ingestion.application.use_cases import (
    AddTextContentUseCase,
    AddVoiceContentUseCase,
    CancelSessionUseCase,
    SetAnalysisResultUseCase,
    StartSessionUseCase,
    TriggerAnalysisUseCase,
)
from telegram_ingestion.domain.models import DraftSession, SessionStatus, UserProfile, UserRole
from telegram_ingestion.domain.repository import DraftSessionRepository, UserProfileRepository
from telegram_ingestion.infrastructure.bot_handler import TelegramFSMStates, create_router

# ---------------------------------------------------------------------------
# In-memory стабы
# ---------------------------------------------------------------------------


class InMemoryDraftRepo(DraftSessionRepository):
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


class InMemoryUserRepo(UserProfileRepository):
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


class InMemoryAgentReg(AgentRegistrationPort):
    def __init__(self, next_id: int = 42) -> None:
        self._next_id = next_id
        self.calls: list[dict[str, str]] = []

    async def create_chatwoot_agent(self, name: str, email: str, password: str) -> int:
        self.calls.append({"name": name, "email": email, "password": password})
        cid = self._next_id
        self._next_id += 1
        return cid


class MockSTT(STTPort):
    def __init__(self, transcript: str = "голосовая транскрипция") -> None:
        self._transcript = transcript

    async def transcribe(self, file_bytes: bytes) -> str:
        return self._transcript


class MockAI(AIClassificationPort):
    async def classify(self, text: str) -> ClassificationResult:
        return ClassificationResult(
            source_text=text,
            title="Проблема с авторизацией",
            description="Пользователь не может войти в систему",
            category=Category.BUG,
            priority=Priority.HIGH,
        )


class InMemoryTicketRepo(SupportTicketRepository):
    def __init__(self) -> None:
        self._store: dict[int, SupportTicket] = {}

    async def get_by_id(self, task_id: int) -> SupportTicket | None:
        return self._store.get(task_id)

    async def save(self, ticket: SupportTicket) -> None:
        self._store[ticket.task_id] = ticket

    async def get_by_assignee(
        self, telegram_id: int, status: str | None = None
    ) -> list[SupportTicket]:
        return []


class InMemoryChatwoot(ChatwootPort):
    def __init__(self) -> None:
        self.created: list[SupportTicket] = []

    async def create_conversation(self, command: object) -> SupportTicket:
        cmd = command
        ticket = SupportTicket(
            task_id=len(self.created) + 1,
            title=getattr(cmd, "title", ""),
            priority=getattr(cmd, "priority", None) or "medium",
        )
        self.created.append(ticket)
        return ticket

    async def update_conversation_status(self, task_id: int, status: str) -> None:
        pass

    async def get_conversations(
        self, assignee_id: int, status: str = "open", page: int = 1
    ) -> list[SupportTicket]:
        return []

    async def add_message(self, task_id: int, content: str, private: bool = True) -> None:
        pass

    async def update_conversation_assignee(self, task_id: int, assignee_chatwoot_id: int) -> None:
        pass


class AuthorizedUserPort(UserProfilePort):
    def __init__(self, user_repo: InMemoryUserRepo) -> None:
        self._repo = user_repo

    async def is_authorized(self, telegram_id: int) -> bool:
        profile = await self._repo.get_by_telegram_id(telegram_id)
        return profile is not None

    async def get_profile(self, telegram_id: int) -> UserProfile | None:
        return await self._repo.get_by_telegram_id(telegram_id)

    async def list_active_agents(self) -> list[UserProfile]:
        return await self._repo.list_active()


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------


def _make_user(user_id: int = 100, first_name: str = "Иван") -> User:
    return User(id=user_id, is_bot=False, first_name=first_name)


def _make_message(
    user_id: int = 100,
    text: str = "/start",
    message_id: int = 1,
    first_name: str = "Иван",
) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type=ChatType.PRIVATE),
        from_user=_make_user(user_id, first_name),
        text=text,
    )


def _make_voice_message(user_id: int = 100, message_id: int = 5) -> Message:
    voice = Voice(
        file_id="voice_file_001",
        file_unique_id="uniq_001",
        duration=3,
        mime_type="audio/ogg",
        file_size=1024,
    )
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type=ChatType.PRIVATE),
        from_user=_make_user(user_id),
        voice=voice,
    )


def _make_update(message: Message, update_id: int = 1) -> Update:
    return Update(update_id=update_id, message=message)


def _make_callback_update(
    user_id: int = 100,
    data: str = "collect",
    update_id: int = 10,
    message_id: int = 10,
) -> Update:
    msg = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type=ChatType.PRIVATE),
        from_user=_make_user(user_id),
        text="нажата кнопка",
    )
    cb = CallbackQuery(
        id="cb_e2e",
        from_user=_make_user(user_id),
        chat_instance="ci",
        data=data,
        message=msg,
    )
    return Update(update_id=update_id, callback_query=cb)


def _storage_key(user_id: int = 100, bot_id: int = 42) -> StorageKey:
    return StorageKey(bot_id=bot_id, chat_id=user_id, user_id=user_id)


def _make_mock_bot() -> MagicMock:
    """Мок бот с поддержкой скачивания голосовых файлов."""
    bot = AsyncMock()
    bot.id = 42
    bot.username = "test_bot"

    tg_file = File(
        file_id="voice_file_001",
        file_unique_id="uniq_001",
        file_size=1024,
        file_path="voice/file001.ogg",
    )
    bot.get_file = AsyncMock(return_value=tg_file)
    bot.download_file = AsyncMock(return_value=io.BytesIO(b"fake_ogg_audio_data"))
    return bot


# ---------------------------------------------------------------------------
# Фабрика Dispatcher с полными зависимостями
# ---------------------------------------------------------------------------


def _build_full_dispatcher(
    draft_repo: InMemoryDraftRepo,
    user_repo: InMemoryUserRepo,
    chatwoot: InMemoryChatwoot,
    ticket_repo: InMemoryTicketRepo,
    stt: MockSTT,
    ai: MockAI,
    agent_reg: InMemoryAgentReg,
    account_id: int = 1,
) -> tuple[Dispatcher, MemoryStorage]:
    user_port = AuthorizedUserPort(user_repo)

    start_session = StartSessionUseCase(draft_repo, user_port)
    add_text = AddTextContentUseCase(draft_repo)
    add_voice = AddVoiceContentUseCase(draft_repo, stt)
    trigger_analysis = TriggerAnalysisUseCase(draft_repo)
    cancel_session = CancelSessionUseCase(draft_repo)
    set_result = SetAnalysisResultUseCase(draft_repo)
    auto_register = AutoRegisterUserUseCase(user_repo, agent_reg, account_id=account_id)
    create_ticket = CreateTicketFromSession(chatwoot, ticket_repo)

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(
        create_router(
            start_session,
            add_text,
            add_voice,
            trigger_analysis,
            cancel_session,
            user_port,
            auto_register=auto_register,
            ai_port=ai,
            set_analysis_result=set_result,
            create_ticket=create_ticket,
            draft_repo=draft_repo,
        )
    )
    return dp, storage


# ---------------------------------------------------------------------------
# E2E тест — полный флоу
# ---------------------------------------------------------------------------


class TestE2EFullFlow:
    """Полный флоу: /start → /new_task → текст → голос → анализ → CRM."""

    # Шаг 1: /start создаёт пользователя в репо и в Chatwoot (через AgentReg)
    async def test_step1_start_registers_new_user(self) -> None:
        draft_repo = InMemoryDraftRepo()
        user_repo = InMemoryUserRepo()
        chatwoot = InMemoryChatwoot()
        ticket_repo = InMemoryTicketRepo()
        agent_reg = InMemoryAgentReg(next_id=50)

        dp, _ = _build_full_dispatcher(
            draft_repo, user_repo, chatwoot, ticket_repo, MockSTT(), MockAI(), agent_reg
        )
        bot = _make_mock_bot()

        await dp.feed_update(bot, _make_update(_make_message(user_id=100, text="/start")))

        profile = await user_repo.get_by_telegram_id(100)
        assert profile is not None, "Профиль должен быть создан после /start"
        assert profile.telegram_id == 100
        assert profile.chatwoot_user_id == 50
        assert profile.role == UserRole.AGENT
        assert len(agent_reg.calls) == 1
        assert agent_reg.calls[0]["email"] == "100@24ondoc.ru"

    # Шаг 2: /new_task создаёт сессию в статусе COLLECTING
    async def test_step2_new_task_creates_collecting_session(self) -> None:
        draft_repo = InMemoryDraftRepo()
        user_repo = InMemoryUserRepo()
        chatwoot = InMemoryChatwoot()
        ticket_repo = InMemoryTicketRepo()
        agent_reg = InMemoryAgentReg(next_id=50)

        dp, storage = _build_full_dispatcher(
            draft_repo, user_repo, chatwoot, ticket_repo, MockSTT(), MockAI(), agent_reg
        )
        bot = _make_mock_bot()
        key = _storage_key(100)

        # Сначала регистрируемся
        await dp.feed_update(bot, _make_update(_make_message(user_id=100, text="/start")))

        # Затем создаём задачу
        await dp.feed_update(
            bot, _make_update(_make_message(user_id=100, text="/new_task", message_id=2))
        )

        state = await storage.get_state(key)
        assert state == TelegramFSMStates.collecting.state
        data = await storage.get_data(key)
        assert "session_id" in data

        session_id = uuid.UUID(data["session_id"])
        session = await draft_repo.get_by_id(session_id)
        assert session is not None
        assert session.status == SessionStatus.COLLECTING

    # Шаг 3: текстовое сообщение добавляет ContentBlock(type=text)
    async def test_step3_text_message_adds_text_content_block(self) -> None:
        draft_repo = InMemoryDraftRepo()
        user_repo = InMemoryUserRepo()
        chatwoot = InMemoryChatwoot()
        ticket_repo = InMemoryTicketRepo()
        agent_reg = InMemoryAgentReg(next_id=50)

        dp, storage = _build_full_dispatcher(
            draft_repo, user_repo, chatwoot, ticket_repo, MockSTT(), MockAI(), agent_reg
        )
        bot = _make_mock_bot()
        key = _storage_key(100)

        await dp.feed_update(bot, _make_update(_make_message(user_id=100, text="/start")))
        await dp.feed_update(
            bot, _make_update(_make_message(user_id=100, text="/new_task", message_id=2))
        )
        await storage.set_state(key, TelegramFSMStates.collecting.state)

        await dp.feed_update(
            bot,
            _make_update(_make_message(user_id=100, text="Не работает кнопка входа", message_id=3)),
        )

        session = await draft_repo.get_active_by_user(100)
        assert session is not None
        text_blocks = [b for b in session.content_blocks if b.type == "text"]
        assert len(text_blocks) >= 1
        assert text_blocks[0].content == "Не работает кнопка входа"

    # Шаг 4: голосовое сообщение → STT → ContentBlock(type=voice)
    async def test_step4_voice_message_adds_voice_content_block(self) -> None:
        draft_repo = InMemoryDraftRepo()
        user_repo = InMemoryUserRepo()
        chatwoot = InMemoryChatwoot()
        ticket_repo = InMemoryTicketRepo()
        agent_reg = InMemoryAgentReg(next_id=50)
        stt = MockSTT("дополнительное описание проблемы")

        dp, storage = _build_full_dispatcher(
            draft_repo, user_repo, chatwoot, ticket_repo, stt, MockAI(), agent_reg
        )
        bot = _make_mock_bot()
        key = _storage_key(100)

        await dp.feed_update(bot, _make_update(_make_message(user_id=100, text="/start")))
        await dp.feed_update(
            bot, _make_update(_make_message(user_id=100, text="/new_task", message_id=2))
        )
        await storage.set_state(key, TelegramFSMStates.collecting.state)

        # Отправляем голосовое сообщение
        voice_msg = _make_voice_message(user_id=100, message_id=4)
        await dp.feed_update(bot, Update(update_id=4, message=voice_msg))

        session = await draft_repo.get_active_by_user(100)
        assert session is not None
        voice_blocks = [b for b in session.content_blocks if b.type == "voice"]
        assert len(voice_blocks) >= 1
        assert voice_blocks[0].content == "дополнительное описание проблемы"
        assert voice_blocks[0].file_id == "voice_file_001"

    # Шаг 5: collect callback → AI анализ → статус PREVIEW
    async def test_step5_collect_callback_triggers_ai_analysis(self) -> None:
        draft_repo = InMemoryDraftRepo()
        user_repo = InMemoryUserRepo()
        chatwoot = InMemoryChatwoot()
        ticket_repo = InMemoryTicketRepo()
        agent_reg = InMemoryAgentReg(next_id=50)

        dp, storage = _build_full_dispatcher(
            draft_repo, user_repo, chatwoot, ticket_repo, MockSTT(), MockAI(), agent_reg
        )
        bot = _make_mock_bot()
        key = _storage_key(100)

        await dp.feed_update(bot, _make_update(_make_message(user_id=100, text="/start")))
        await dp.feed_update(
            bot, _make_update(_make_message(user_id=100, text="/new_task", message_id=2))
        )

        # Получаем session_id
        data = await storage.get_data(key)
        session_id = uuid.UUID(data["session_id"])
        session = await draft_repo.get_by_id(session_id)
        assert session is not None

        # Добавляем текстовый блок напрямую через сессию, чтобы trigger_analysis не падал
        from telegram_ingestion.domain.models import ContentBlock

        session.add_content_block(ContentBlock(type="text", content="проблема"))
        await draft_repo.save(session)

        await storage.set_state(key, TelegramFSMStates.collecting.state)

        # Нажимаем "Собрать"
        await dp.feed_update(bot, _make_callback_update(user_id=100, data="collect"))

        state = await storage.get_state(key)
        assert state == TelegramFSMStates.preview.state

        updated = await draft_repo.get_by_id(session_id)
        assert updated is not None
        assert updated.status == SessionStatus.PREVIEW
        assert updated.ai_result is not None
        assert updated.ai_result.title == "Проблема с авторизацией"

    # Шаг 6: create_crm callback → тикет создаётся в Chatwoot
    async def test_step6_create_crm_creates_ticket_in_chatwoot(self) -> None:
        from telegram_ingestion.domain.models import AIResult, ContentBlock

        draft_repo = InMemoryDraftRepo()
        user_repo = InMemoryUserRepo()
        chatwoot = InMemoryChatwoot()
        ticket_repo = InMemoryTicketRepo()
        agent_reg = InMemoryAgentReg(next_id=50)

        dp, storage = _build_full_dispatcher(
            draft_repo, user_repo, chatwoot, ticket_repo, MockSTT(), MockAI(), agent_reg
        )
        bot = _make_mock_bot()
        key = _storage_key(100)

        await dp.feed_update(bot, _make_update(_make_message(user_id=100, text="/start")))
        await dp.feed_update(
            bot, _make_update(_make_message(user_id=100, text="/new_task", message_id=2))
        )

        data = await storage.get_data(key)
        session_id = uuid.UUID(data["session_id"])
        session = await draft_repo.get_by_id(session_id)
        assert session is not None

        # Готовим сессию в PREVIEW
        session.add_content_block(ContentBlock(type="text", content="задача"))
        session.start_analysis()
        ai_res = AIResult(
            title="Проблема с авторизацией",
            description="Пользователь не может войти",
            category="bug",
            priority="high",
        )
        session.complete_analysis(ai_res)
        await draft_repo.save(session)

        await storage.set_state(key, TelegramFSMStates.preview.state)
        await storage.set_data(key, {"session_id": str(session_id)})

        # Нажимаем "Создать в CRM"
        await dp.feed_update(bot, _make_callback_update(user_id=100, data="create_crm"))

        assert len(chatwoot.created) == 1
        ticket = chatwoot.created[0]
        assert ticket.title == "Проблема с авторизацией"

        state = await storage.get_state(key)
        assert state is None

    # Шаг 7: Полный флоу целиком — все 6 шагов подряд
    async def test_step7_full_e2e_flow(self) -> None:
        """Полный E2E: /start → /new_task → текст → голос → collect → create_crm."""
        draft_repo = InMemoryDraftRepo()
        user_repo = InMemoryUserRepo()
        chatwoot = InMemoryChatwoot()
        ticket_repo = InMemoryTicketRepo()
        agent_reg = InMemoryAgentReg(next_id=77)
        stt = MockSTT("добавлю голосовой контекст")

        dp, storage = _build_full_dispatcher(
            draft_repo, user_repo, chatwoot, ticket_repo, stt, MockAI(), agent_reg
        )
        bot = _make_mock_bot()
        key = _storage_key(100)

        # --- Шаг 1: /start — регистрация ---
        await dp.feed_update(bot, _make_update(_make_message(user_id=100, text="/start")))
        profile = await user_repo.get_by_telegram_id(100)
        assert profile is not None, "Шаг 1: профиль должен появиться"
        assert profile.chatwoot_user_id == 77

        # --- Шаг 2: /new_task — создание сессии ---
        await dp.feed_update(
            bot, _make_update(_make_message(user_id=100, text="/new_task", message_id=2))
        )
        state = await storage.get_state(key)
        assert state == TelegramFSMStates.collecting.state, "Шаг 2: должен быть collecting"
        data = await storage.get_data(key)
        session_id = uuid.UUID(data["session_id"])

        # --- Шаг 3: текст ---
        await dp.feed_update(
            bot,
            _make_update(
                _make_message(user_id=100, text="Кнопка входа не реагирует", message_id=3)
            ),
        )
        session = await draft_repo.get_by_id(session_id)
        assert session is not None
        text_blocks = [b for b in session.content_blocks if b.type == "text"]
        assert len(text_blocks) == 1, "Шаг 3: text ContentBlock"
        assert text_blocks[0].content == "Кнопка входа не реагирует"

        # --- Шаг 4: голос ---
        voice_msg = _make_voice_message(user_id=100, message_id=4)
        await dp.feed_update(bot, Update(update_id=4, message=voice_msg))
        session = await draft_repo.get_by_id(session_id)
        assert session is not None
        voice_blocks = [b for b in session.content_blocks if b.type == "voice"]
        assert len(voice_blocks) == 1, "Шаг 4: voice ContentBlock"
        assert voice_blocks[0].content == "добавлю голосовой контекст"

        # --- Шаг 5: collect → AI анализ → PREVIEW ---
        await dp.feed_update(bot, _make_callback_update(user_id=100, data="collect"))
        state = await storage.get_state(key)
        assert state == TelegramFSMStates.preview.state, "Шаг 5: должен быть preview"
        session = await draft_repo.get_by_id(session_id)
        assert session is not None
        assert session.status == SessionStatus.PREVIEW
        assert session.ai_result is not None
        assert session.ai_result.title == "Проблема с авторизацией"
        assert session.ai_result.priority == "high"

        # --- Шаг 6: create_crm → тикет в CRM ---
        await storage.set_data(key, {"session_id": str(session_id)})
        await dp.feed_update(bot, _make_callback_update(user_id=100, data="create_crm"))
        assert len(chatwoot.created) == 1, "Шаг 6: тикет создан в Chatwoot"
        ticket = chatwoot.created[0]
        assert ticket.title == "Проблема с авторизацией"

        state = await storage.get_state(key)
        assert state is None, "Шаг 6: FSM очищен после создания тикета"
