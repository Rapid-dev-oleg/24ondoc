"""Tests for T2 Webhook handler (DEV-50)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from ..domain.models import CallRecord, CallStatus
from ..domain.repository import CallRecordRepository
from ..infrastructure.webhook_handler import router


# ---------- Helpers ----------

_VALID_SECRET = "test-t2-secret"

_VALID_PAYLOAD = {
    "call_id": "t2_test_001",
    "audio_url": "https://t2.example.com/rec/001.mp3",
    "caller_phone": "+79991234567",
    "agent_ext": "101",
}


def _build_app(
    repo: CallRecordRepository | None = None,
    secret: str | None = _VALID_SECRET,
    process_fn: object | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def inject_state(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.call_repo = repo
        request.state.t2_webhook_secret = secret
        request.state.process_call_fn = process_fn
        return await call_next(request)

    return app


# ---------- Tests ----------


class TestT2WebhookHandler:
    def test_webhook_valid_secret_accepts_call(self) -> None:
        """AC: валидный секрет → 200 + CallRecord создан."""
        repo = AsyncMock(spec=CallRecordRepository)
        repo.save = AsyncMock()
        client = TestClient(_build_app(repo=repo))

        response = client.post(
            "/webhook/t2/call",
            json=_VALID_PAYLOAD,
            headers={"X-T2-Secret": _VALID_SECRET},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "accepted"
        assert data["call_id"] == "t2_test_001"
        repo.save.assert_awaited_once()

    def test_webhook_invalid_secret_returns_401(self) -> None:
        """AC: неверный секрет → 401."""
        repo = AsyncMock(spec=CallRecordRepository)
        client = TestClient(_build_app(repo=repo))

        response = client.post(
            "/webhook/t2/call",
            json=_VALID_PAYLOAD,
            headers={"X-T2-Secret": "wrong-secret"},
        )

        assert response.status_code == 401
        repo.save.assert_not_called()

    def test_webhook_missing_secret_returns_401(self) -> None:
        """Нет заголовка X-T2-Secret → 401."""
        repo = AsyncMock(spec=CallRecordRepository)
        client = TestClient(_build_app(repo=repo))

        response = client.post("/webhook/t2/call", json=_VALID_PAYLOAD)

        assert response.status_code == 401

    def test_webhook_missing_required_fields_returns_422(self) -> None:
        """AC: неполный payload → 422."""
        repo = AsyncMock(spec=CallRecordRepository)
        client = TestClient(_build_app(repo=repo))

        # Missing required fields
        response = client.post(
            "/webhook/t2/call",
            json={"call_id": "t2_001"},
            headers={"X-T2-Secret": _VALID_SECRET},
        )

        assert response.status_code == 422

    def test_webhook_background_task_triggered(self) -> None:
        """AC: background task запущена."""
        repo = AsyncMock(spec=CallRecordRepository)
        repo.save = AsyncMock()
        process_fn = MagicMock()
        client = TestClient(_build_app(repo=repo, process_fn=process_fn))

        response = client.post(
            "/webhook/t2/call",
            json=_VALID_PAYLOAD,
            headers={"X-T2-Secret": _VALID_SECRET},
        )

        assert response.status_code == 200
        process_fn.assert_called_once_with("t2_test_001")

    def test_webhook_no_background_fn_still_succeeds(self) -> None:
        """Если process_fn не задан — всё равно 200."""
        repo = AsyncMock(spec=CallRecordRepository)
        repo.save = AsyncMock()
        client = TestClient(_build_app(repo=repo, process_fn=None))

        response = client.post(
            "/webhook/t2/call",
            json=_VALID_PAYLOAD,
            headers={"X-T2-Secret": _VALID_SECRET},
        )

        assert response.status_code == 200

    def test_webhook_with_optional_fields(self) -> None:
        """Необязательные поля (transcription_t2, duration) принимаются."""
        repo = AsyncMock(spec=CallRecordRepository)
        repo.save = AsyncMock()
        client = TestClient(_build_app(repo=repo))

        payload = {**_VALID_PAYLOAD, "transcription_t2": "Тест", "duration": 120}
        response = client.post(
            "/webhook/t2/call",
            json=payload,
            headers={"X-T2-Secret": _VALID_SECRET},
        )

        assert response.status_code == 200
        saved: CallRecord = repo.save.call_args[0][0]
        assert saved.transcription_t2 == "Тест"
        assert saved.duration == 120

    def test_webhook_creates_call_record_with_new_status(self) -> None:
        """Созданный CallRecord имеет статус NEW."""
        repo = AsyncMock(spec=CallRecordRepository)
        repo.save = AsyncMock()
        client = TestClient(_build_app(repo=repo))

        client.post(
            "/webhook/t2/call",
            json=_VALID_PAYLOAD,
            headers={"X-T2-Secret": _VALID_SECRET},
        )

        saved: CallRecord = repo.save.call_args[0][0]
        assert saved.status == CallStatus.NEW
        assert saved.call_id == "t2_test_001"
        assert saved.caller_phone == "+79991234567"

    def test_webhook_repo_unavailable_returns_503(self) -> None:
        """Если repo недоступен → 503."""
        client = TestClient(_build_app(repo=None))

        response = client.post(
            "/webhook/t2/call",
            json=_VALID_PAYLOAD,
            headers={"X-T2-Secret": _VALID_SECRET},
        )

        assert response.status_code == 503
