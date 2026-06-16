"""Stage 1 — Agent/Admin action guards on task inline buttons.

Verifies that callback handlers for task_resolve / task_reopen / task_reassign
/ task_comment refuse actions for AGENT role and pass through for ADMIN.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.telegram_ingestion.domain.models import UserProfile, UserRole
from src.telegram_ingestion.infrastructure.bot_handler import create_tasks_router


def _make_callback(from_user_id: int | None, data: str) -> Any:
    cb = MagicMock()
    cb.from_user = MagicMock(id=from_user_id) if from_user_id is not None else None
    cb.data = data
    cb.answer = AsyncMock()
    cb.message = None  # bail out before any message edit
    return cb


def _build_router(profile: UserProfile | None):
    user_port = MagicMock()
    user_port.get_profile = AsyncMock(return_value=profile)
    user_port.list_active_agents = AsyncMock(return_value=[])
    router = create_tasks_router(
        get_my_tasks=MagicMock(),
        update_task_status=MagicMock(execute=AsyncMock(return_value=True)),
        reassign_task=MagicMock(execute=AsyncMock(return_value=True)),
        add_task_comment=MagicMock(execute=AsyncMock()),
        user_port=user_port,
    )
    return router, user_port


def _find_handler(router, predicate_str: str):
    for observer in router.observers.values():
        for handler in observer.handlers:
            if predicate_str in str(handler.callback.__name__):
                return handler.callback
    raise AssertionError(f"Handler matching {predicate_str!r} not found")


def _agent() -> UserProfile:
    return UserProfile(telegram_id=100, role=UserRole.AGENT)


def _admin() -> UserProfile:
    return UserProfile(telegram_id=200, role=UserRole.ADMIN)


@pytest.mark.asyncio
async def test_agent_cannot_resolve_task() -> None:
    router, _ = _build_router(_agent())
    handler = _find_handler(router, "task_resolve")
    state = MagicMock(get_data=AsyncMock(return_value={"current_task_id": 42}))
    cb = _make_callback(from_user_id=100, data="task_resolve")

    await handler(cb, state)

    cb.answer.assert_called_once()
    args, kwargs = cb.answer.call_args
    assert "администратор" in (args[0] if args else kwargs.get("text", "")).lower()
    assert kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_agent_cannot_reopen_task() -> None:
    router, _ = _build_router(_agent())
    handler = _find_handler(router, "task_reopen")
    state = MagicMock(get_data=AsyncMock(return_value={"current_task_id": 42}))
    cb = _make_callback(from_user_id=100, data="task_reopen")

    await handler(cb, state)

    cb.answer.assert_called_once()
    _, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_agent_cannot_open_reassign_list() -> None:
    router, _ = _build_router(_agent())
    handler = _find_handler(router, "reassign_list")
    state = MagicMock(get_data=AsyncMock(return_value={}))
    cb = _make_callback(from_user_id=100, data="task_reassign_list:42")

    await handler(cb, state)

    _, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_agent_cannot_reassign_to_specific_agent() -> None:
    router, _ = _build_router(_agent())
    handler = _find_handler(router, "reassign_to")
    state = MagicMock(get_data=AsyncMock(return_value={}))
    cb = _make_callback(from_user_id=100, data="reassign_to:42:999")

    await handler(cb, state)

    _, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_agent_cannot_comment_task() -> None:
    router, _ = _build_router(_agent())
    handler = _find_handler(router, "task_comment")
    state = MagicMock(set_state=AsyncMock(), update_data=AsyncMock())
    cb = _make_callback(from_user_id=100, data="task_comment:42")

    await handler(cb, state)

    _, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True
    state.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_admin_can_resolve_task() -> None:
    router, _ = _build_router(_admin())
    handler = _find_handler(router, "task_resolve")
    state = MagicMock(get_data=AsyncMock(return_value={"current_task_id": 42, "tasks": [], "current_task_idx": 0}))
    cb = _make_callback(from_user_id=200, data="task_resolve")

    await handler(cb, state)

    # Not blocked — the answer call we care about is the success path.
    # Since cb.message is None we don't try to edit; only .answer runs.
    # The first call should NOT include show_alert=True (only guards use show_alert).
    for call in cb.answer.call_args_list:
        _, kwargs = call
        assert kwargs.get("show_alert") is not True or "администратор" not in (
            call.args[0] if call.args else kwargs.get("text", "")
        ).lower()


@pytest.mark.asyncio
async def test_unauthenticated_callback_is_rejected_silently() -> None:
    router, user_port = _build_router(None)
    handler = _find_handler(router, "task_resolve")
    state = MagicMock(get_data=AsyncMock(return_value={"current_task_id": 42}))
    cb = _make_callback(from_user_id=None, data="task_resolve")

    await handler(cb, state)

    cb.answer.assert_called_once()
    user_port.get_profile.assert_not_called()
