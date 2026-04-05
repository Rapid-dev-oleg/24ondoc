"""Tests for STORY-119: Рефакторинг UserProfile — добавить twenty_member_id.

Acceptance criteria:
  1. AutoRegisterUserUseCase.execute() создаёт UserProfile только с telegram_id и role
  2. Если профиль уже есть, возвращает существующий без создания нового
  3. У UserProfile нет устаревших CRM-атрибутов
  4. Поле twenty_member_id: str | None присутствует
"""

from __future__ import annotations

from datetime import UTC, datetime

from telegram_ingestion.application.registration_use_cases import AutoRegisterUserUseCase
from telegram_ingestion.domain.models import UserProfile, UserRole
from telegram_ingestion.domain.repository import UserProfileRepository

# ---------------------------------------------------------------------------
# In-memory stub
# ---------------------------------------------------------------------------


class InMemoryUserProfileRepo(UserProfileRepository):
    def __init__(self) -> None:
        self._store: dict[int, UserProfile] = {}

    async def get_by_telegram_id(self, telegram_id: int) -> UserProfile | None:
        return self._store.get(telegram_id)

    async def save(self, profile: UserProfile) -> None:
        self._store[profile.telegram_id] = profile

    async def list_active(self) -> list[UserProfile]:
        return [p for p in self._store.values() if p.is_active]

    async def list_all(self) -> list[UserProfile]:
        return list(self._store.values())

    async def delete_by_telegram_id(self, telegram_id: int) -> None:
        self._store.pop(telegram_id, None)


# ---------------------------------------------------------------------------
# AC1: AutoRegisterUserUseCase creates minimal UserProfile
# ---------------------------------------------------------------------------


class TestAutoRegisterCreatesMinimalProfile:
    async def test_auto_register_creates_user_profile(self) -> None:
        repo = InMemoryUserProfileRepo()
        uc = AutoRegisterUserUseCase(repo)

        profile, is_new = await uc.execute(telegram_id=42, first_name="Алиса")

        assert is_new is True
        assert profile.telegram_id == 42
        assert profile.role == UserRole.AGENT
        # Legacy CRM fields must not exist
        assert not hasattr(profile, "chatwoot_user_id")
        assert not hasattr(profile, "chatwoot_account_id")
        assert not hasattr(profile, "chatwoot_contact_id")


# ---------------------------------------------------------------------------
# AC2: Returns existing profile if found
# ---------------------------------------------------------------------------


class TestAutoRegisterReturnsExisting:
    async def test_auto_register_returns_existing_profile_if_found(self) -> None:
        repo = InMemoryUserProfileRepo()
        existing = UserProfile(
            telegram_id=10,
            role=UserRole.AGENT,
            is_active=True,
            created_at=datetime.now(UTC),
        )
        await repo.save(existing)
        uc = AutoRegisterUserUseCase(repo)

        profile, is_new = await uc.execute(telegram_id=10, first_name="Боб")

        assert is_new is False
        assert profile.telegram_id == 10


# ---------------------------------------------------------------------------
# AC3: UserProfile has no legacy CRM fields
# ---------------------------------------------------------------------------


class TestUserProfileHasNoLegacyCRMFields:
    def test_user_profile_has_no_legacy_crm_fields(self) -> None:
        profile = UserProfile(telegram_id=1, role=UserRole.AGENT)
        assert not hasattr(profile, "chatwoot_user_id")
        assert not hasattr(profile, "chatwoot_account_id")
        assert not hasattr(profile, "chatwoot_contact_id")


# ---------------------------------------------------------------------------
# AC4: UserProfile has twenty_member_id
# ---------------------------------------------------------------------------


class TestUserProfileHasTwentyMemberId:
    def test_user_profile_has_twenty_member_id(self) -> None:
        profile = UserProfile(telegram_id=1, role=UserRole.AGENT)
        assert profile.twenty_member_id is None

        profile_with_id = UserProfile(
            telegram_id=2,
            role=UserRole.AGENT,
            twenty_member_id="550e8400-e29b-41d4-a716-446655440000",
        )
        assert profile_with_id.twenty_member_id == "550e8400-e29b-41d4-a716-446655440000"
