"""Unit-тесты для domain events TelegramIngestion."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from ..domain.events import (
    DomainEvent,
    MessageReceived,
    SessionAnalysisCompleted,
    SessionReadyForAnalysis,
    TaskCreatedInCRM,
    VoiceReceived,
)
from ..domain.models import UserProfile, UserRole


class TestDomainEvents:
    def test_domain_event_has_occurred_at(self) -> None:
        event = DomainEvent()
        assert isinstance(event.occurred_at, datetime)
        assert event.occurred_at.tzinfo is not None

    def test_message_received_defaults(self) -> None:
        event = MessageReceived()
        assert event.content_type == "text"
        assert event.user_id == 0

    def test_message_received_with_values(self) -> None:
        sid = uuid.uuid4()
        event = MessageReceived(session_id=sid, user_id=42, content_type="voice")
        assert event.session_id == sid
        assert event.user_id == 42
        assert event.content_type == "voice"

    def test_voice_received_defaults(self) -> None:
        event = VoiceReceived()
        assert event.file_id == ""
        assert event.user_id == 0

    def test_voice_received_with_values(self) -> None:
        sid = uuid.uuid4()
        event = VoiceReceived(session_id=sid, user_id=7, file_id="file_abc")
        assert event.file_id == "file_abc"
        assert event.user_id == 7

    def test_session_ready_for_analysis_defaults(self) -> None:
        event = SessionReadyForAnalysis()
        assert event.assembled_text == ""

    def test_session_ready_for_analysis_with_text(self) -> None:
        event = SessionReadyForAnalysis(assembled_text="Проблема с оплатой", user_id=5)
        assert event.assembled_text == "Проблема с оплатой"
        assert event.user_id == 5

    def test_session_analysis_completed(self) -> None:
        sid = uuid.uuid4()
        event = SessionAnalysisCompleted(session_id=sid, user_id=3)
        assert event.session_id == sid
        assert event.user_id == 3

    def test_task_created_in_crm(self) -> None:
        sid = uuid.uuid4()
        event = TaskCreatedInCRM(session_id=sid, user_id=1)
        assert event.user_id == 1

    def test_events_are_frozen(self) -> None:
        event = MessageReceived(user_id=10)
        with pytest.raises((AttributeError, TypeError)):
            event.user_id = 99  # type: ignore[misc]


class TestUserProfileModel:
    def test_user_profile_default_role(self) -> None:
        profile = UserProfile(telegram_id=1)
        assert profile.role == UserRole.AGENT

    def test_user_role_supervisor_value(self) -> None:
        assert UserRole.SUPERVISOR.value == "supervisor"

    def test_user_role_admin_value(self) -> None:
        assert UserRole.ADMIN.value == "admin"

    def test_user_profile_is_active_default(self) -> None:
        profile = UserProfile(telegram_id=1)
        assert profile.is_active is True
