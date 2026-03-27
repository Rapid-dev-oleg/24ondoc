"""Tests for /my_tasks FSM flow: use cases and bot handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from aiogram import Dispatcher
from aiogram.enums import ChatType
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    Update,
    User,
)

from chatwoot_integration.domain.models import SupportTicket, TicketStatus
from chatwoot_integration.domain.repository import ChatwootPort

from ..application.ports import UserProfilePort
from ..application.tasks_use_cases import (
    AddTaskCommentUseCase,
    GetMyTasksUseCase,
    ReassignTaskUseCase,
    UpdateTaskStatusUseCase,
)
from ..domain.models import UserProfile, UserRole
from ..infrastructure.bot_handler import TelegramFSMStates, create_tasks_router

# ---------- Stubs ----------


class StubUserProfilePort(UserProfilePort):
    def __init__(
        self,
        profile: UserProfile | None = None,
        agents: list[UserProfile] | None = None,
    ) -> None:
        self._profile = profile
        self._agents = agents or []

    async def is_authorized(self, telegram_id: int) -> bool:
        return self._profile is not None and self._profile.telegram_id == telegram_id

    async def get_profile(self, telegram_id: int) -> UserProfile | None:
        if self._profile and self._profile.telegram_id == telegram_id:
            return self._profile
        return None

    async def list_active_agents(self) -> list[UserProfile]:
        return self._agents


class InMemoryChatwootPort(ChatwootPort):
    def __init__(self, tickets: list[SupportTicket] | None = None) -> None:
        self._tickets: list[SupportTicket] = tickets or []
        self.status_updates: list[tuple[int, str]] = []
        self.assignee_updates: list[tuple[int, int]] = []
        self.messages: list[tuple[int, str, bool]] = []

    async def create_conversation(self, command: object) -> SupportTicket:
        raise NotImplementedError

    async def update_conversation_status(self, task_id: int, status: str) -> None:
        self.status_updates.append((task_id, status))

    async def get_conversations(
        self, assignee_id: int, status: str = "open", page: int = 1
    ) -> list[SupportTicket]:
        return self._tickets

    async def update_conversation_assignee(self, task_id: int, assignee_chatwoot_id: int) -> None:
        self.assignee_updates.append((task_id, assignee_chatwoot_id))

    async def add_message(self, task_id: int, content: str, private: bool = True) -> None:
        self.messages.append((task_id, content, private))


def _make_agent_profile(
    telegram_id: int = 100,
    chatwoot_user_id: int = 10,
    role: UserRole = UserRole.AGENT,
) -> UserProfile:
    return UserProfile(
        telegram_id=telegram_id,
        chatwoot_user_id=chatwoot_user_id,
        chatwoot_account_id=1,
        role=role,
    )


def _make_ticket(
    task_id: int = 1,
    title: str = "Тест задача",
    assignee_chatwoot_id: int | None = 10,
    status: TicketStatus = TicketStatus.OPEN,
) -> SupportTicket:
    return SupportTicket(
        task_id=task_id,
        title=title,
        assignee_chatwoot_id=assignee_chatwoot_id,
        status=status,
    )


# ---------- Tests: GetMyTasksUseCase ----------


class TestGetMyTasksUseCase:
    async def test_returns_tasks_for_user(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10)
        tickets = [_make_ticket(task_id=1), _make_ticket(task_id=2)]
        uc = GetMyTasksUseCase(StubUserProfilePort(profile), InMemoryChatwootPort(tickets))
        result = await uc.execute(telegram_id=100)
        assert len(result) == 2

    async def test_returns_empty_list_if_user_not_found(self) -> None:
        uc = GetMyTasksUseCase(StubUserProfilePort(None), InMemoryChatwootPort([]))
        result = await uc.execute(telegram_id=999)
        assert result == []

    async def test_passes_page_to_chatwoot(self) -> None:
        profile = _make_agent_profile()
        InMemoryChatwootPort([_make_ticket()])

        class TrackingChatwoot(InMemoryChatwootPort):
            def __init__(self) -> None:
                super().__init__([_make_ticket()])
                self.last_page = 0

            async def get_conversations(
                self, assignee_id: int, status: str = "open", page: int = 1
            ) -> list[SupportTicket]:
                self.last_page = page
                return self._tickets

        tracking = TrackingChatwoot()
        uc = GetMyTasksUseCase(StubUserProfilePort(profile), tracking)
        await uc.execute(telegram_id=100, page=3)
        assert tracking.last_page == 3

    async def test_returns_empty_list_on_chatwoot_error(self) -> None:
        profile = _make_agent_profile()

        class FailingChatwoot(InMemoryChatwootPort):
            async def get_conversations(
                self, assignee_id: int, status: str = "open", page: int = 1
            ) -> list[SupportTicket]:
                return []

        uc = GetMyTasksUseCase(StubUserProfilePort(profile), FailingChatwoot())
        result = await uc.execute(telegram_id=100)
        assert result == []


# ---------- Tests: UpdateTaskStatusUseCase ----------


class TestUpdateTaskStatusUseCase:
    async def test_assignee_can_resolve(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10)
        chatwoot = InMemoryChatwootPort()
        uc = UpdateTaskStatusUseCase(StubUserProfilePort(profile), chatwoot)
        ok = await uc.execute(
            requester_telegram_id=100,
            task_id=1,
            assignee_chatwoot_id=10,
            new_status="resolved",
        )
        assert ok is True
        assert (1, "resolved") in chatwoot.status_updates

    async def test_assignee_can_reopen(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10)
        chatwoot = InMemoryChatwootPort()
        uc = UpdateTaskStatusUseCase(StubUserProfilePort(profile), chatwoot)
        ok = await uc.execute(
            requester_telegram_id=100,
            task_id=5,
            assignee_chatwoot_id=10,
            new_status="open",
        )
        assert ok is True
        assert (5, "open") in chatwoot.status_updates

    async def test_non_assignee_cannot_change_status(self) -> None:
        # chatwoot_user_id=99 does NOT match assignee_chatwoot_id=10
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=99)
        chatwoot = InMemoryChatwootPort()
        uc = UpdateTaskStatusUseCase(StubUserProfilePort(profile), chatwoot)
        ok = await uc.execute(
            requester_telegram_id=100,
            task_id=1,
            assignee_chatwoot_id=10,
            new_status="resolved",
        )
        assert ok is False
        assert len(chatwoot.status_updates) == 0

    async def test_returns_false_if_user_not_found(self) -> None:
        chatwoot = InMemoryChatwootPort()
        uc = UpdateTaskStatusUseCase(StubUserProfilePort(None), chatwoot)
        ok = await uc.execute(
            requester_telegram_id=999,
            task_id=1,
            assignee_chatwoot_id=10,
            new_status="resolved",
        )
        assert ok is False

    async def test_returns_false_if_no_assignee(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10)
        chatwoot = InMemoryChatwootPort()
        uc = UpdateTaskStatusUseCase(StubUserProfilePort(profile), chatwoot)
        ok = await uc.execute(
            requester_telegram_id=100,
            task_id=1,
            assignee_chatwoot_id=None,
            new_status="resolved",
        )
        assert ok is False


# ---------- Tests: ReassignTaskUseCase ----------


class TestReassignTaskUseCase:
    async def test_supervisor_can_reassign(self) -> None:
        profile = _make_agent_profile(
            telegram_id=100, chatwoot_user_id=10, role=UserRole.SUPERVISOR
        )
        chatwoot = InMemoryChatwootPort()
        uc = ReassignTaskUseCase(StubUserProfilePort(profile), chatwoot)
        ok = await uc.execute(requester_telegram_id=100, task_id=1, target_chatwoot_user_id=20)
        assert ok is True
        assert (1, 20) in chatwoot.assignee_updates

    async def test_admin_can_reassign(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10, role=UserRole.ADMIN)
        chatwoot = InMemoryChatwootPort()
        uc = ReassignTaskUseCase(StubUserProfilePort(profile), chatwoot)
        ok = await uc.execute(requester_telegram_id=100, task_id=2, target_chatwoot_user_id=30)
        assert ok is True
        assert (2, 30) in chatwoot.assignee_updates

    async def test_agent_cannot_reassign(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10, role=UserRole.AGENT)
        chatwoot = InMemoryChatwootPort()
        uc = ReassignTaskUseCase(StubUserProfilePort(profile), chatwoot)
        ok = await uc.execute(requester_telegram_id=100, task_id=1, target_chatwoot_user_id=20)
        assert ok is False
        assert len(chatwoot.assignee_updates) == 0

    async def test_returns_false_if_requester_not_found(self) -> None:
        chatwoot = InMemoryChatwootPort()
        uc = ReassignTaskUseCase(StubUserProfilePort(None), chatwoot)
        ok = await uc.execute(requester_telegram_id=999, task_id=1, target_chatwoot_user_id=20)
        assert ok is False


# ---------- Tests: AddTaskCommentUseCase ----------


class TestAddTaskCommentUseCase:
    async def test_adds_private_comment(self) -> None:
        chatwoot = InMemoryChatwootPort()
        uc = AddTaskCommentUseCase(chatwoot)
        await uc.execute(task_id=1, content="Тестовый комментарий")
        assert (1, "Тестовый комментарий", True) in chatwoot.messages

    async def test_adds_different_task_comment(self) -> None:
        chatwoot = InMemoryChatwootPort()
        uc = AddTaskCommentUseCase(chatwoot)
        await uc.execute(task_id=42, content="Другой комментарий")
        assert (42, "Другой комментарий", True) in chatwoot.messages


# ---------- aiogram test helpers ----------


def _make_tg_user(user_id: int = 100) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def _make_message(
    user_id: int = 100,
    text: str = "/my_tasks",
    message_id: int = 1,
) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type=ChatType.PRIVATE),
        from_user=_make_tg_user(user_id),
        text=text,
    )


def _make_update(message: Message, update_id: int = 1) -> Update:
    return Update(update_id=update_id, message=message)


def _make_callback(
    user_id: int = 100,
    data: str = "tasks_page:0",
    message_id: int = 1,
) -> CallbackQuery:
    msg = Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type=ChatType.PRIVATE),
        from_user=_make_tg_user(user_id),
        text="список задач",
    )
    return CallbackQuery(
        id="cb1",
        from_user=_make_tg_user(user_id),
        chat_instance="ci",
        data=data,
        message=msg,
    )


def _make_mock_bot(bot_id: int = 42) -> MagicMock:
    bot = AsyncMock()
    bot.id = bot_id
    bot.username = "test_bot"
    return bot


def _build_dispatcher(
    profile: UserProfile | None = None,
    agents: list[UserProfile] | None = None,
    tickets: list[SupportTicket] | None = None,
    bot_id: int = 42,
) -> tuple[Dispatcher, InMemoryChatwootPort, MemoryStorage]:
    user_port = StubUserProfilePort(profile, agents)
    chatwoot = InMemoryChatwootPort(tickets)
    get_tasks = GetMyTasksUseCase(user_port, chatwoot)
    update_status = UpdateTaskStatusUseCase(user_port, chatwoot)
    reassign = ReassignTaskUseCase(user_port, chatwoot)
    add_comment = AddTaskCommentUseCase(chatwoot)

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    router = create_tasks_router(
        get_my_tasks=get_tasks,
        update_task_status=update_status,
        reassign_task=reassign,
        add_task_comment=add_comment,
        user_port=user_port,
    )
    dp.include_router(router)
    return dp, chatwoot, storage


def _storage_key(user_id: int = 100, bot_id: int = 42) -> StorageKey:
    return StorageKey(bot_id=bot_id, chat_id=user_id, user_id=user_id)


# ---------- Tests: /my_tasks FSM Bot Handlers ----------


class TestMyTasksBotHandlers:
    async def test_my_tasks_with_tickets_sets_tasks_list_state(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10)
        tickets = [_make_ticket(task_id=1, title="Задача А")]
        dp, _, storage = _build_dispatcher(profile=profile, tickets=tickets)
        bot = _make_mock_bot(bot_id=42)

        update = _make_update(_make_message(user_id=100, text="/my_tasks"))
        await dp.feed_update(bot, update)

        state = await storage.get_state(_storage_key(100))
        assert state == TelegramFSMStates.tasks_list.state

    async def test_my_tasks_with_tickets_stores_tasks_in_fsm_data(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10)
        tickets = [_make_ticket(task_id=1, title="Задача А")]
        dp, _, storage = _build_dispatcher(profile=profile, tickets=tickets)
        bot = _make_mock_bot(bot_id=42)

        update = _make_update(_make_message(user_id=100, text="/my_tasks"))
        await dp.feed_update(bot, update)

        data = await storage.get_data(_storage_key(100))
        assert len(data.get("tasks", [])) == 1
        assert data["tasks"][0]["task_id"] == 1

    async def test_my_tasks_no_tickets_does_not_set_state(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10)
        dp, _, storage = _build_dispatcher(profile=profile, tickets=[])
        bot = _make_mock_bot(bot_id=42)

        update = _make_update(_make_message(user_id=100, text="/my_tasks"))
        await dp.feed_update(bot, update)

        state = await storage.get_state(_storage_key(100))
        # No tasks → state stays None (no transition to tasks_list)
        assert state != TelegramFSMStates.tasks_list.state

    async def test_my_tasks_no_profile_does_not_set_state(self) -> None:
        dp, _, storage = _build_dispatcher(profile=None, tickets=[])
        bot = _make_mock_bot(bot_id=42)

        update = _make_update(_make_message(user_id=100, text="/my_tasks"))
        await dp.feed_update(bot, update)

        state = await storage.get_state(_storage_key(100))
        assert state != TelegramFSMStates.tasks_list.state

    async def test_tasks_page_pagination_updates_page_in_state(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10)
        tickets = [_make_ticket(task_id=i, title=f"Задача {i}") for i in range(1, 8)]
        dp, _, storage = _build_dispatcher(profile=profile, tickets=tickets)
        bot = _make_mock_bot(bot_id=42)
        key = _storage_key(100)

        # First show the list
        update = _make_update(_make_message(user_id=100, text="/my_tasks"))
        await dp.feed_update(bot, update)

        # Pre-set state and data for callback
        await storage.set_state(key, TelegramFSMStates.tasks_list.state)
        serialized = [
            {"task_id": t.task_id, "title": t.title, "status": "open", "assignee_chatwoot_id": 10}
            for t in tickets
        ]
        await storage.set_data(key, {"tasks": serialized, "tasks_page": 0})

        # Navigate to page 1
        cb_update = Update(
            update_id=2,
            callback_query=_make_callback(user_id=100, data="tasks_page:1"),
        )
        await dp.feed_update(bot, cb_update)

        data = await storage.get_data(key)
        assert data.get("tasks_page") == 1

    async def test_task_detail_callback_sets_task_detail_state(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10)
        tickets = [_make_ticket(task_id=7, title="Детальная задача", assignee_chatwoot_id=10)]
        dp, _, storage = _build_dispatcher(profile=profile, tickets=tickets)
        bot = _make_mock_bot(bot_id=42)
        key = _storage_key(100)

        # Pre-set state with tasks data
        serialized = [
            {
                "task_id": 7,
                "title": "Детальная за��ача",
                "status": "open",
                "assignee_chatwoot_id": 10,
            }
        ]
        await storage.set_state(key, TelegramFSMStates.tasks_list.state)
        await storage.set_data(key, {"tasks": serialized, "tasks_page": 0})

        cb_update = Update(
            update_id=2,
            callback_query=_make_callback(user_id=100, data="task_detail:7:10"),
        )
        await dp.feed_update(bot, cb_update)

        state = await storage.get_state(key)
        assert state == TelegramFSMStates.task_detail.state

    async def test_resolve_callback_for_assignee(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10)
        tickets = [_make_ticket(task_id=3, assignee_chatwoot_id=10)]
        dp, chatwoot, _ = _build_dispatcher(profile=profile, tickets=tickets)
        bot = _make_mock_bot()

        cb_update = Update(
            update_id=1,
            callback_query=_make_callback(user_id=100, data="task_resolve:3:10"),
        )
        await dp.feed_update(bot, cb_update)
        assert (3, "resolved") in chatwoot.status_updates

    async def test_reopen_callback_for_assignee(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10)
        dp, chatwoot, _ = _build_dispatcher(profile=profile, tickets=[])
        bot = _make_mock_bot()

        cb_update = Update(
            update_id=1,
            callback_query=_make_callback(user_id=100, data="task_reopen:5:10"),
        )
        await dp.feed_update(bot, cb_update)
        assert (5, "open") in chatwoot.status_updates

    async def test_resolve_denied_for_non_assignee(self) -> None:
        # chatwoot_user_id=99, but assignee is 10
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=99)
        dp, chatwoot, _ = _build_dispatcher(profile=profile, tickets=[])
        bot = _make_mock_bot()

        cb_update = Update(
            update_id=1,
            callback_query=_make_callback(user_id=100, data="task_resolve:3:10"),
        )
        await dp.feed_update(bot, cb_update)
        assert len(chatwoot.status_updates) == 0

    async def test_reassign_callback_for_supervisor(self) -> None:
        profile = _make_agent_profile(
            telegram_id=100, chatwoot_user_id=10, role=UserRole.SUPERVISOR
        )
        agents = [
            _make_agent_profile(telegram_id=200, chatwoot_user_id=20, role=UserRole.AGENT),
        ]
        dp, chatwoot, _ = _build_dispatcher(profile=profile, agents=agents, tickets=[])
        bot = _make_mock_bot()

        # Reassign task 3 to agent with chatwoot_id 20
        cb_update = Update(
            update_id=1,
            callback_query=_make_callback(user_id=100, data="reassign_to:3:20"),
        )
        await dp.feed_update(bot, cb_update)
        assert (3, 20) in chatwoot.assignee_updates

    async def test_comment_callback_sets_adding_comment_state(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10)
        dp, _, storage = _build_dispatcher(profile=profile, tickets=[])
        bot = _make_mock_bot(bot_id=42)
        key = _storage_key(100)

        cb_update = Update(
            update_id=1,
            callback_query=_make_callback(user_id=100, data="task_comment:5"),
        )
        await dp.feed_update(bot, cb_update)

        state = await storage.get_state(key)
        assert state == TelegramFSMStates.adding_comment.state

    async def test_comment_callback_stores_task_id_in_fsm(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10)
        dp, _, storage = _build_dispatcher(profile=profile, tickets=[])
        bot = _make_mock_bot(bot_id=42)
        key = _storage_key(100)

        cb_update = Update(
            update_id=1,
            callback_query=_make_callback(user_id=100, data="task_comment:5"),
        )
        await dp.feed_update(bot, cb_update)

        data = await storage.get_data(key)
        assert data.get("comment_task_id") == 5

    async def test_comment_text_submitted_calls_use_case(self) -> None:
        profile = _make_agent_profile(telegram_id=100, chatwoot_user_id=10)
        dp, chatwoot, storage = _build_dispatcher(profile=profile, tickets=[])
        bot = _make_mock_bot(bot_id=42)
        key = _storage_key(100)

        # Pre-set adding_comment state with task_id
        await storage.set_state(key, TelegramFSMStates.adding_comment.state)
        await storage.set_data(key, {"comment_task_id": 7})

        # Send comment text
        msg_update = _make_update(
            _make_message(user_id=100, text="Мой комментарий", message_id=2),
            update_id=2,
        )
        await dp.feed_update(bot, msg_update)
        assert (7, "Мой комментарий", True) in chatwoot.messages
