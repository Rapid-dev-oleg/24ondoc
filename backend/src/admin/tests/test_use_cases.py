"""Admin panel — TDD tests for use cases."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from admin.application.ports import ChatwootAdminPort, EnvSettingsPort
from admin.application.use_cases import (
    AddPendingUseCase,
    CreateOperatorUseCase,
    DeactivateUserUseCase,
    DeletePendingUseCase,
    GetSettingsUseCase,
    ListPendingUseCase,
    ListUsersUseCase,
    UpdateSettingsUseCase,
    UpdateUserUseCase,
    _mask_value,
)
from admin.domain.models import (
    AddPendingRequest,
    CreateUserRequest,
    UpdateSettingsRequest,
    UpdateUserRequest,
)
from admin.infrastructure.auth import create_access_token, decode_access_token
from telegram_ingestion.domain.models import PendingUser, UserProfile, UserRole
from telegram_ingestion.domain.repository import PendingUserRepository, UserProfileRepository


# ---------------------------------------------------------------------------
# In-memory stubs (reused across tests)
# ---------------------------------------------------------------------------


class InMemoryUserProfileRepository(UserProfileRepository):
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


class InMemoryChatwootAdminPort(ChatwootAdminPort):
    def __init__(self, next_id: int = 100) -> None:
        self._next_id = next_id
        self.calls: list[dict[str, str]] = []

    async def create_agent(self, name: str, email: str, role: str) -> int:
        self.calls.append({"name": name, "email": email, "role": role})
        result = self._next_id
        self._next_id += 1
        return result


class InMemoryEnvSettingsPort(EnvSettingsPort):
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self._data: dict[str, str] = dict(data or {})

    def get_setting(self, key: str) -> str | None:
        return self._data.get(key)

    def update_setting(self, key: str, value: str) -> None:
        self._data[key] = value


def _make_user(
    telegram_id: int = 1,
    chatwoot_user_id: int = 10,
    role: UserRole = UserRole.ADMIN,
    is_active: bool = True,
) -> UserProfile:
    return UserProfile(
        telegram_id=telegram_id,
        chatwoot_user_id=chatwoot_user_id,
        chatwoot_account_id=1,
        role=role,
        is_active=is_active,
        created_at=datetime.now(timezone.utc),
    )


def _make_pending(phone: str = "79001234567", chatwoot_user_id: int = 20) -> PendingUser:
    return PendingUser(
        phone=phone,
        chatwoot_user_id=chatwoot_user_id,
        chatwoot_account_id=1,
        role=UserRole.AGENT,
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Tests: _mask_value
# ---------------------------------------------------------------------------


class TestMaskValue:
    def test_masks_long_value(self) -> None:
        # "sk-abcdefgh1234" is 15 chars → 11 stars + last 4
        assert _mask_value("sk-abcdefgh1234") == "***********1234"

    def test_exactly_4_chars_fully_hidden(self) -> None:
        # Values <= 4 chars are fully masked to not expose the full short secret
        assert _mask_value("abcd") == "****"

    def test_less_than_4_chars(self) -> None:
        assert _mask_value("abc") == "****"

    def test_empty_string(self) -> None:
        assert _mask_value("") == "****"


# ---------------------------------------------------------------------------
# Tests: JWT
# ---------------------------------------------------------------------------


class TestJWT:
    _SECRET = "test-secret-key"

    def test_create_and_decode(self) -> None:
        token = create_access_token(42, "admin", self._SECRET)
        payload = decode_access_token(token, self._SECRET)
        assert payload["sub"] == "42"
        assert payload["role"] == "admin"

    def test_invalid_token_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid token"):
            decode_access_token("not.a.token", self._SECRET)

    def test_wrong_secret_raises(self) -> None:
        token = create_access_token(1, "admin", self._SECRET)
        with pytest.raises(ValueError, match="Invalid token"):
            decode_access_token(token, "wrong-secret")


# ---------------------------------------------------------------------------
# Tests: ListUsersUseCase
# ---------------------------------------------------------------------------


class TestListUsersUseCase:
    async def test_returns_active_users_and_pending(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        pending_repo = InMemoryPendingUserRepository()

        user = _make_user(telegram_id=1)
        pending = _make_pending(phone="79001234567")
        await user_repo.save(user)
        await pending_repo.save(pending)

        uc = ListUsersUseCase(user_repo, pending_repo)
        result = await uc.execute()

        assert len(result) == 2
        active = [r for r in result if not r.is_pending]
        pend = [r for r in result if r.is_pending]
        assert len(active) == 1
        assert len(pend) == 1
        assert active[0].telegram_id == 1
        assert pend[0].phone == "79001234567"

    async def test_inactive_user_excluded(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        pending_repo = InMemoryPendingUserRepository()

        inactive = _make_user(telegram_id=2, is_active=False)
        await user_repo.save(inactive)

        uc = ListUsersUseCase(user_repo, pending_repo)
        result = await uc.execute()
        assert result == []

    async def test_empty_repositories(self) -> None:
        uc = ListUsersUseCase(InMemoryUserProfileRepository(), InMemoryPendingUserRepository())
        assert await uc.execute() == []


# ---------------------------------------------------------------------------
# Tests: CreateOperatorUseCase
# ---------------------------------------------------------------------------


class TestCreateOperatorUseCase:
    async def test_creates_agent_and_saves_pending(self) -> None:
        chatwoot = InMemoryChatwootAdminPort(next_id=200)
        pending_repo = InMemoryPendingUserRepository()

        uc = CreateOperatorUseCase(chatwoot, pending_repo, account_id=1)
        req = CreateUserRequest(phone="+79001234567", name="John", email="j@test.com")
        result = await uc.execute(req)

        assert result.phone == "79001234567"
        assert result.chatwoot_user_id == 200
        assert len(chatwoot.calls) == 1
        assert chatwoot.calls[0]["name"] == "John"

        saved = await pending_repo.get_by_phone("79001234567")
        assert saved is not None
        assert saved.chatwoot_user_id == 200

    async def test_phone_normalized(self) -> None:
        chatwoot = InMemoryChatwootAdminPort(next_id=300)
        pending_repo = InMemoryPendingUserRepository()

        uc = CreateOperatorUseCase(chatwoot, pending_repo, account_id=1)
        req = CreateUserRequest(phone="89001234567", name="Jane", email="jane@test.com")
        result = await uc.execute(req)

        assert result.phone == "79001234567"

    async def test_chatwoot_error_propagates(self) -> None:
        class FailingChatwoot(ChatwootAdminPort):
            async def create_agent(self, name: str, email: str, role: str) -> int:
                raise RuntimeError("Chatwoot unavailable")

        uc = CreateOperatorUseCase(FailingChatwoot(), InMemoryPendingUserRepository(), account_id=1)
        req = CreateUserRequest(phone="79000000000", name="X", email="x@x.com")
        with pytest.raises(RuntimeError, match="Chatwoot unavailable"):
            await uc.execute(req)


# ---------------------------------------------------------------------------
# Tests: UpdateUserUseCase
# ---------------------------------------------------------------------------


class TestUpdateUserUseCase:
    async def test_update_role(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        user = _make_user(role=UserRole.AGENT)
        await user_repo.save(user)

        uc = UpdateUserUseCase(user_repo)
        result = await uc.execute(1, UpdateUserRequest(role=UserRole.SUPERVISOR))

        assert result is not None
        assert result.role == UserRole.SUPERVISOR
        saved = await user_repo.get_by_telegram_id(1)
        assert saved is not None
        assert saved.role == UserRole.SUPERVISOR

    async def test_update_is_active(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        user = _make_user(is_active=True)
        await user_repo.save(user)

        uc = UpdateUserUseCase(user_repo)
        result = await uc.execute(1, UpdateUserRequest(is_active=False))

        assert result is not None
        assert result.is_active is False

    async def test_user_not_found_returns_none(self) -> None:
        uc = UpdateUserUseCase(InMemoryUserProfileRepository())
        result = await uc.execute(999, UpdateUserRequest(role=UserRole.ADMIN))
        assert result is None


# ---------------------------------------------------------------------------
# Tests: DeactivateUserUseCase
# ---------------------------------------------------------------------------


class TestDeactivateUserUseCase:
    async def test_deactivates_existing_user(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        user = _make_user(is_active=True)
        await user_repo.save(user)

        uc = DeactivateUserUseCase(user_repo)
        found = await uc.execute(1)

        assert found is True
        saved = await user_repo.get_by_telegram_id(1)
        assert saved is not None
        assert saved.is_active is False

    async def test_returns_false_for_missing_user(self) -> None:
        uc = DeactivateUserUseCase(InMemoryUserProfileRepository())
        assert await uc.execute(999) is False


# ---------------------------------------------------------------------------
# Tests: ListPendingUseCase
# ---------------------------------------------------------------------------


class TestListPendingUseCase:
    async def test_returns_all_pending(self) -> None:
        pending_repo = InMemoryPendingUserRepository()
        await pending_repo.save(_make_pending("79001111111", 10))
        await pending_repo.save(_make_pending("79002222222", 20))

        uc = ListPendingUseCase(pending_repo)
        result = await uc.execute()

        assert len(result) == 2
        phones = {r.phone for r in result}
        assert phones == {"79001111111", "79002222222"}

    async def test_empty(self) -> None:
        uc = ListPendingUseCase(InMemoryPendingUserRepository())
        assert await uc.execute() == []


# ---------------------------------------------------------------------------
# Tests: AddPendingUseCase
# ---------------------------------------------------------------------------


class TestAddPendingUseCase:
    async def test_adds_and_normalizes_phone(self) -> None:
        pending_repo = InMemoryPendingUserRepository()
        uc = AddPendingUseCase(pending_repo)
        req = AddPendingRequest(
            phone="+7 900 123-45-67", chatwoot_user_id=50, chatwoot_account_id=1
        )
        result = await uc.execute(req)

        assert result.phone == "79001234567"
        assert result.chatwoot_user_id == 50

    async def test_overwrites_existing_phone(self) -> None:
        pending_repo = InMemoryPendingUserRepository()
        await pending_repo.save(_make_pending("79001234567", chatwoot_user_id=10))

        uc = AddPendingUseCase(pending_repo)
        req = AddPendingRequest(
            phone="79001234567", chatwoot_user_id=99, chatwoot_account_id=1
        )
        result = await uc.execute(req)

        assert result.chatwoot_user_id == 99
        saved = await pending_repo.get_by_phone("79001234567")
        assert saved is not None and saved.chatwoot_user_id == 99


# ---------------------------------------------------------------------------
# Tests: DeletePendingUseCase
# ---------------------------------------------------------------------------


class TestDeletePendingUseCase:
    async def test_deletes_existing(self) -> None:
        pending_repo = InMemoryPendingUserRepository()
        await pending_repo.save(_make_pending("79001234567"))

        uc = DeletePendingUseCase(pending_repo)
        found = await uc.execute("79001234567")

        assert found is True
        assert await pending_repo.get_by_phone("79001234567") is None

    async def test_normalizes_phone_before_delete(self) -> None:
        pending_repo = InMemoryPendingUserRepository()
        await pending_repo.save(_make_pending("79001234567"))

        uc = DeletePendingUseCase(pending_repo)
        found = await uc.execute("+79001234567")

        assert found is True

    async def test_returns_false_for_missing(self) -> None:
        uc = DeletePendingUseCase(InMemoryPendingUserRepository())
        assert await uc.execute("79009999999") is False


# ---------------------------------------------------------------------------
# Tests: GetSettingsUseCase
# ---------------------------------------------------------------------------


class TestGetSettingsUseCase:
    def test_masks_keys(self) -> None:
        env = InMemoryEnvSettingsPort(
            {
                "OPENROUTER_API_KEY": "sk-abcdef1234",
                "TELEGRAM_BOT_TOKEN": "1234567:ABCDEFTOKEN",
            }
        )
        result = GetSettingsUseCase(env).execute()
        assert result.openrouter_api_key.endswith("1234")
        assert "*" in result.openrouter_api_key
        assert result.telegram_bot_token.endswith("OKEN")

    def test_missing_keys_return_all_masked(self) -> None:
        env = InMemoryEnvSettingsPort({})
        result = GetSettingsUseCase(env).execute()
        assert result.openrouter_api_key == "****"
        assert result.telegram_bot_token == "****"


# ---------------------------------------------------------------------------
# Tests: UpdateSettingsUseCase
# ---------------------------------------------------------------------------


class TestUpdateSettingsUseCase:
    def test_updates_openrouter_key(self) -> None:
        env = InMemoryEnvSettingsPort({"OPENROUTER_API_KEY": "old-key-value"})
        uc = UpdateSettingsUseCase(env)
        result = uc.execute(UpdateSettingsRequest(openrouter_api_key="new-key-abcd"))

        assert result.openrouter_api_key.endswith("abcd")
        assert env.get_setting("OPENROUTER_API_KEY") == "new-key-abcd"

    def test_updates_telegram_token(self) -> None:
        env = InMemoryEnvSettingsPort({"TELEGRAM_BOT_TOKEN": "old:TOKEN"})
        uc = UpdateSettingsUseCase(env)
        uc.execute(UpdateSettingsRequest(telegram_bot_token="123:NEWTOKEN"))

        assert env.get_setting("TELEGRAM_BOT_TOKEN") == "123:NEWTOKEN"

    def test_partial_update_leaves_other_unchanged(self) -> None:
        env = InMemoryEnvSettingsPort(
            {"OPENROUTER_API_KEY": "keepme1234", "TELEGRAM_BOT_TOKEN": "keeptoken"}
        )
        uc = UpdateSettingsUseCase(env)
        uc.execute(UpdateSettingsRequest(openrouter_api_key="newkey5678"))

        assert env.get_setting("TELEGRAM_BOT_TOKEN") == "keeptoken"
        assert env.get_setting("OPENROUTER_API_KEY") == "newkey5678"
