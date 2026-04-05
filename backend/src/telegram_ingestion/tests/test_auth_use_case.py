"""Tests for phone authorization use cases (TDD)."""

from __future__ import annotations

from ..application.auth_use_case import AuthByPhoneUseCase, RegisterPhoneUseCase, normalize_phone
from ..application.ports import UserProfilePort
from ..domain.models import PendingUser, UserProfile, UserRole
from ..domain.repository import PendingUserRepository, UserProfileRepository

# ---------------------------------------------------------------------------
# In-memory stubs
# ---------------------------------------------------------------------------


class InMemoryPendingUserRepository(PendingUserRepository):
    def __init__(self) -> None:
        self._store: dict[str, PendingUser] = {}

    async def get_by_phone(self, phone: str) -> PendingUser | None:
        return self._store.get(phone)

    async def save(self, pending: PendingUser) -> None:
        self._store[pending.phone] = pending

    async def delete(self, phone: str) -> None:
        self._store.pop(phone, None)

    async def list_all(self) -> list[PendingUser]:
        return list(self._store.values())


class InMemoryUserProfileRepository(UserProfileRepository):
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


class InMemoryUserProfilePort(UserProfilePort):
    def __init__(self, profiles: dict[int, UserProfile] | None = None) -> None:
        self._profiles = profiles or {}

    async def is_authorized(self, telegram_id: int) -> bool:
        return telegram_id in self._profiles

    async def get_profile(self, telegram_id: int) -> UserProfile | None:
        return self._profiles.get(telegram_id)

    async def list_active_agents(self) -> list[UserProfile]:
        return list(self._profiles.values())

    async def update_twenty_member_id(
        self, telegram_id: int, twenty_member_id: str
    ) -> UserProfile | None:
        return None


# ---------------------------------------------------------------------------
# Tests: normalize_phone
# ---------------------------------------------------------------------------


class TestNormalizePhone:
    def test_strips_plus(self) -> None:
        assert normalize_phone("+79001234567") == "79001234567"

    def test_strips_spaces_and_dashes(self) -> None:
        assert normalize_phone("+7 900 123-45-67") == "79001234567"

    def test_converts_8_to_7_for_russian(self) -> None:
        assert normalize_phone("89001234567") == "79001234567"

    def test_already_normalized(self) -> None:
        assert normalize_phone("79001234567") == "79001234567"

    def test_strips_parentheses(self) -> None:
        assert normalize_phone("+7(900)1234567") == "79001234567"

    def test_non_russian_8_not_converted(self) -> None:
        # 8-digit number starting with 8 — not 11 digits, no conversion
        assert normalize_phone("812345678") == "812345678"

    def test_short_number_unchanged(self) -> None:
        assert normalize_phone("12345") == "12345"


# ---------------------------------------------------------------------------
# Tests: AuthByPhoneUseCase
# ---------------------------------------------------------------------------


class TestAuthByPhoneUseCase:
    async def test_success_creates_profile_and_deletes_pending(self) -> None:
        pending_repo = InMemoryPendingUserRepository()
        user_repo = InMemoryUserProfileRepository()
        pending = PendingUser(
            phone="79001234567",
            role=UserRole.AGENT,
        )
        await pending_repo.save(pending)

        uc = AuthByPhoneUseCase(pending_repo, user_repo)
        profile = await uc.execute(telegram_id=100, phone="+79001234567")

        assert profile is not None
        assert profile.telegram_id == 100
        assert profile.role == UserRole.AGENT

        # Pending record deleted
        assert await pending_repo.get_by_phone("79001234567") is None
        # User profile saved
        saved = await user_repo.get_by_telegram_id(100)
        assert saved is not None
        assert saved.telegram_id == 100

    async def test_not_found_returns_none(self) -> None:
        pending_repo = InMemoryPendingUserRepository()
        user_repo = InMemoryUserProfileRepository()

        uc = AuthByPhoneUseCase(pending_repo, user_repo)
        result = await uc.execute(telegram_id=999, phone="+79999999999")

        assert result is None
        # No profile saved
        assert await user_repo.get_by_telegram_id(999) is None

    async def test_phone_normalized_before_lookup(self) -> None:
        pending_repo = InMemoryPendingUserRepository()
        user_repo = InMemoryUserProfileRepository()
        pending = PendingUser(
            phone="79001234567",
        )
        await pending_repo.save(pending)

        uc = AuthByPhoneUseCase(pending_repo, user_repo)
        # Phone given as 8... format
        profile = await uc.execute(telegram_id=200, phone="89001234567")

        assert profile is not None
        assert profile.telegram_id == 200


# ---------------------------------------------------------------------------
# Tests: RegisterPhoneUseCase
# ---------------------------------------------------------------------------


class TestRegisterPhoneUseCase:
    async def test_admin_can_register_phone(self) -> None:
        pending_repo = InMemoryPendingUserRepository()
        admin = UserProfile(
            telegram_id=1,
            role=UserRole.ADMIN,
        )
        user_port = InMemoryUserProfilePort({1: admin})

        uc = RegisterPhoneUseCase(pending_repo, user_port)
        ok = await uc.execute(
            requester_telegram_id=1,
            phone="+79001234567",
        )

        assert ok is True
        pending = await pending_repo.get_by_phone("79001234567")
        assert pending is not None

    async def test_supervisor_can_register_phone(self) -> None:
        pending_repo = InMemoryPendingUserRepository()
        supervisor = UserProfile(
            telegram_id=2,
            role=UserRole.SUPERVISOR,
        )
        user_port = InMemoryUserProfilePort({2: supervisor})

        uc = RegisterPhoneUseCase(pending_repo, user_port)
        ok = await uc.execute(
            requester_telegram_id=2,
            phone="+79001111111",
        )

        assert ok is True
        pending = await pending_repo.get_by_phone("79001111111")
        assert pending is not None

    async def test_agent_cannot_register_phone(self) -> None:
        pending_repo = InMemoryPendingUserRepository()
        agent = UserProfile(
            telegram_id=3,
            role=UserRole.AGENT,
        )
        user_port = InMemoryUserProfilePort({3: agent})

        uc = RegisterPhoneUseCase(pending_repo, user_port)
        ok = await uc.execute(
            requester_telegram_id=3,
            phone="+79002222222",
        )

        assert ok is False
        assert await pending_repo.get_by_phone("79002222222") is None

    async def test_unknown_user_cannot_register_phone(self) -> None:
        pending_repo = InMemoryPendingUserRepository()
        user_port = InMemoryUserProfilePort({})  # no users

        uc = RegisterPhoneUseCase(pending_repo, user_port)
        ok = await uc.execute(
            requester_telegram_id=999,
            phone="+79003333333",
        )

        assert ok is False
        assert await pending_repo.get_by_phone("79003333333") is None
