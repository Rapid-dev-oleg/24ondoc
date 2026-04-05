"""Tests for ATS2 configuration and app startup integration (DEV-129)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestSettingsLoadsAts2Config:
    """AC: test_settings_loads_ats2_config"""

    def test_settings_loads_ats2_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Settings must load ATS2_ENABLED, ATS2_POLL_INTERVAL_SEC with correct defaults."""
        # Minimum required env vars for Settings
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "fake-secret")
        monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
        monkeypatch.setenv("T2_WEBHOOK_SECRET", "fake-t2")

        # ATS2-specific
        monkeypatch.setenv("ATS2_ACCESS_TOKEN", "test-access")
        monkeypatch.setenv("ATS2_REFRESH_TOKEN", "test-refresh")
        monkeypatch.setenv("ATS2_BASE_URL", "https://custom.ats2.url/api")
        monkeypatch.setenv("ATS2_POLL_INTERVAL_SEC", "30")
        monkeypatch.setenv("ATS2_ENABLED", "true")

        from config import Settings

        settings = Settings()  # type: ignore[call-arg]

        assert settings.ats2_access_token == "test-access"
        assert settings.ats2_refresh_token == "test-refresh"
        assert settings.ats2_base_url == "https://custom.ats2.url/api"
        assert settings.ats2_poll_interval_sec == 30
        assert settings.ats2_enabled is True

    def test_settings_ats2_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ATS2_ENABLED defaults to False, ATS2_POLL_INTERVAL_SEC defaults to 60."""
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "fake-secret")
        monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
        monkeypatch.setenv("T2_WEBHOOK_SECRET", "fake-t2")

        from config import Settings

        settings = Settings()  # type: ignore[call-arg]

        assert settings.ats2_enabled is False
        assert settings.ats2_poll_interval_sec == 60


class TestPollerNotStartedWhenDisabled:
    """AC: test_poller_not_started_when_disabled"""

    @pytest.mark.asyncio
    async def test_poller_not_started_when_disabled(self) -> None:
        """When ATS2_ENABLED=false, ATS2PollerService must NOT be started in lifespan."""
        from main import _create_ats2_poller

        settings = MagicMock()
        settings.ats2_enabled = False

        result = _create_ats2_poller(settings, session_factory=MagicMock())
        assert result is None


class TestPollerStartsOnAppStartup:
    """AC: test_poller_starts_on_app_startup"""

    @pytest.mark.asyncio
    async def test_poller_starts_on_app_startup(self) -> None:
        """When ATS2_ENABLED=true, ATS2PollerService is created with correct params."""
        from main import _create_ats2_poller

        settings = MagicMock()
        settings.ats2_enabled = True
        settings.ats2_base_url = "https://ats2.t2.ru/crm/openapi"
        settings.ats2_access_token = "access-tok"
        settings.ats2_refresh_token = "refresh-tok"
        settings.ats2_poll_interval_sec = 45

        session_factory = MagicMock()

        poller = _create_ats2_poller(settings, session_factory=session_factory)
        assert poller is not None
        assert poller._poll_interval_sec == 45
