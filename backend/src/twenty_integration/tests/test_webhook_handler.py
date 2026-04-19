"""Stage 5 — /webhook/twenty handler tests.

Run the FastAPI app with request.state primed like the real lifespan would.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.task_events.domain.models import Action, Source, TaskEvent
from src.twenty_integration.infrastructure.webhook_handler import router as twenty_router


def _app(write_event: object, secret: str = "s3cret") -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.write_task_event = write_event
        request.state.twenty_webhook_secret = secret
        return await call_next(request)

    app.include_router(twenty_router)
    return app


def _mock_write(events: list[TaskEvent] | None = None) -> MagicMock:
    we = MagicMock()
    we.execute = AsyncMock()
    # `_is_recent_echo` reaches into `_repo.recent_by_task`
    we._repo = MagicMock()
    we._repo.recent_by_task = AsyncMock(return_value=events or [])
    return we


async def _post(app: FastAPI, body: dict, secret: str = "s3cret") -> tuple[int, dict]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/webhook/twenty", json=body, headers={"X-Twenty-Secret": secret})
        return r.status_code, r.json()


@pytest.mark.asyncio
async def test_rejects_missing_secret() -> None:
    write = _mock_write()
    app = _app(write)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/webhook/twenty", json={"eventType": "task.created", "recordId": "x"})
    assert r.status_code == 401
    write.execute.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_wrong_secret() -> None:
    write = _mock_write()
    app = _app(write, secret="right")
    status, _ = await _post(app, {"eventType": "task.created", "recordId": "x"}, secret="wrong")
    assert status == 401
    write.execute.assert_not_called()


@pytest.mark.asyncio
async def test_empty_secret_disables_auth() -> None:
    """If TWENTY_WEBHOOK_SECRET is empty (not configured), the handler
    accepts any request — matches the case where the operator created
    the webhook in Twenty UI without setting a secret."""
    write = _mock_write()
    app = _app(write, secret="")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/webhook/twenty",
                         json={"eventName": "task.created", "recordId": "task-xyz"})
    assert r.status_code == 200
    write.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_created_becomes_created_event() -> None:
    write = _mock_write()
    app = _app(write)
    status, body = await _post(app, {"eventType": "task.created", "recordId": "task-1"})
    assert status == 200 and body["status"] == "ok"
    write.execute.assert_awaited_once()
    kwargs = write.execute.call_args.kwargs
    assert kwargs["action"] == Action.CREATED
    assert kwargs["twenty_task_id"] == "task-1"
    assert kwargs["source"] == Source.WEBHOOK


@pytest.mark.asyncio
async def test_task_updated_with_assignee_field_becomes_assigned() -> None:
    write = _mock_write()
    app = _app(write)
    status, _ = await _post(
        app,
        {"eventType": "task.updated", "recordId": "t-2", "updatedFields": ["assigneeId"]},
    )
    assert status == 200
    assert write.execute.call_args.kwargs["action"] == Action.ASSIGNED


@pytest.mark.asyncio
async def test_task_deleted_becomes_cancelled() -> None:
    write = _mock_write()
    app = _app(write)
    status, _ = await _post(app, {"eventType": "task.deleted", "recordId": "t-3"})
    assert status == 200
    assert write.execute.call_args.kwargs["action"] == Action.CANCELLED


@pytest.mark.asyncio
async def test_payload_without_task_id_is_ignored() -> None:
    write = _mock_write()
    app = _app(write)
    status, body = await _post(app, {"eventType": "task.created"})
    assert status == 200 and body["status"] == "ignored"
    write.execute.assert_not_called()


@pytest.mark.asyncio
async def test_echo_within_window_is_deduped() -> None:
    now = datetime.now(UTC)
    echoed = TaskEvent(
        twenty_task_id="t-5",
        action=Action.CREATED,
        source=Source.CALL,     # our own write
        created_at=now - timedelta(seconds=2),
    )
    write = _mock_write([echoed])
    app = _app(write)
    status, body = await _post(app, {"eventType": "task.created", "recordId": "t-5"})
    assert status == 200 and body["status"] == "deduped"
    write.execute.assert_not_called()


@pytest.mark.asyncio
async def test_old_matching_event_is_not_dedup() -> None:
    old = TaskEvent(
        twenty_task_id="t-6",
        action=Action.CREATED,
        source=Source.CALL,
        created_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    write = _mock_write([old])
    app = _app(write)
    status, body = await _post(app, {"eventType": "task.created", "recordId": "t-6"})
    assert status == 200 and body["status"] == "ok"
    write.execute.assert_awaited_once()
