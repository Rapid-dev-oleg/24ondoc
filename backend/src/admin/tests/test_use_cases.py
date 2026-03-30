"""Admin panel — TDD tests for use cases."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime

import pytest

from admin.application.ports import ChatwootAdminPort, EnvSettingsPort, TelegramNotificationPort
from admin.application.use_cases import (
    CreateUserDirectUseCase,
    DeactivateUserUseCase,
    DeleteUserUseCase,
    GetSettingsUseCase,
    ListUsersUseCase,
    LoginWithTelegramUseCase,
    UpdateSettingsUseCase,
    UpdateUserUseCase,
    _mask_value,
    verify_telegram_hash,
)
from admin.domain.models import (
    CreateUserRequest,
    TelegramAuthRequest,
    UpdateSettingsRequest,
    UpdateUserRequest,
)
from admin.infrastructure.auth import create_access_token, decode_access_token
from telegram_ingestion.domain.models import UserProfile, UserRole
from telegram_ingestion.domain.repository import UserProfileRepository

# ---------------------------------------------------------------------------
# In-memory stubs (reused across tests)
# ---------------------------------------------------------------------------


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


class InMemoryChatwootAdminPort(ChatwootAdminPort):
    def __init__(self, next_id: int = 100) -> None:
        self._next_id = next_id
        self.calls: list[dict[str, str]] = []
        self.deleted_agent_ids: list[int] = []
        self.delete_raises: Exception | None = None

    async def create_agent(self, name: str, email: str, role: str) -> int:
        self.calls.append({"name": name, "email": email, "role": role})
        result = self._next_id
        self._next_id += 1
        return result

    async def delete_agent(self, chatwoot_user_id: int) -> None:
        if self.delete_raises is not None:
            raise self.delete_raises
        self.deleted_agent_ids.append(chatwoot_user_id)


class InMemoryEnvSettingsPort(EnvSettingsPort):
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self._data: dict[str, str] = dict(data or {})

    def get_setting(self, key: str) -> str | None:
        return self._data.get(key)

    def update_setting(self, key: str, value: str) -> None:
        self._data[key] = value


class InMemoryTelegramNotificationPort(TelegramNotificationPort):
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, telegram_id: int, text: str) -> None:
        self.sent.append((telegram_id, text))


def _make_user(
    telegram_id: int = 1,
    role: UserRole = UserRole.ADMIN,
    is_active: bool = True,
) -> UserProfile:
    return UserProfile(
        telegram_id=telegram_id,
        role=role,
        is_active=is_active,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Tests: _mask_value
# ---------------------------------------------------------------------------


class TestMaskValue:
    def test_masks_long_value(self) -> None:
        assert _mask_value("sk-abcdefgh1234") == "***********1234"

    def test_exactly_4_chars_fully_hidden(self) -> None:
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
    async def test_returns_all_users(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        user = _make_user(telegram_id=1)
        await user_repo.save(user)

        uc = ListUsersUseCase(user_repo)
        result = await uc.execute()

        assert len(result) == 1
        assert result[0].telegram_id == 1
        assert result[0].is_pending is False

    async def test_inactive_user_included(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        inactive = _make_user(telegram_id=2, is_active=False)
        await user_repo.save(inactive)

        uc = ListUsersUseCase(user_repo)
        result = await uc.execute()
        assert len(result) == 1
        assert result[0].telegram_id == 2
        assert result[0].is_active is False

    async def test_returns_all_fields(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        from telegram_ingestion.domain.models import UserProfile

        user = UserProfile(
            telegram_id=3,
            twenty_member_id="member-55",
            role=UserRole.AGENT,
            phone_internal="+79001234567",
            voice_sample_url="https://example.com/sample.ogg",
            settings={"lang": "ru"},
            is_active=True,
            created_at=datetime.now(UTC),
        )
        await user_repo.save(user)

        uc = ListUsersUseCase(user_repo)
        result = await uc.execute()

        assert len(result) == 1
        r = result[0]
        assert r.twenty_member_id == "member-55"
        assert r.phone_internal == "+79001234567"
        assert r.voice_sample_url == "https://example.com/sample.ogg"
        assert r.settings == {"lang": "ru"}

    async def test_empty_repository(self) -> None:
        uc = ListUsersUseCase(InMemoryUserProfileRepository())
        assert await uc.execute() == []


# ---------------------------------------------------------------------------
# Tests: CreateUserDirectUseCase
# ---------------------------------------------------------------------------


class TestCreateUserDirectUseCase:
    async def test_creates_user_in_chatwoot_and_repo(self) -> None:
        chatwoot = InMemoryChatwootAdminPort(next_id=200)
        user_repo = InMemoryUserProfileRepository()
        notify = InMemoryTelegramNotificationPort()

        uc = CreateUserDirectUseCase(chatwoot, user_repo, notify, account_id=2)
        req = CreateUserRequest(telegram_id=12345, name="John", email="j@test.com")
        result = await uc.execute(req)

        assert result.telegram_id == 12345
        assert result.is_pending is False

        saved = await user_repo.get_by_telegram_id(12345)
        assert saved is not None

        assert len(chatwoot.calls) == 1
        assert chatwoot.calls[0]["name"] == "John"

    async def test_sends_telegram_notification(self) -> None:
        chatwoot = InMemoryChatwootAdminPort(next_id=300)
        user_repo = InMemoryUserProfileRepository()
        notify = InMemoryTelegramNotificationPort()

        uc = CreateUserDirectUseCase(chatwoot, user_repo, notify, account_id=2)
        req = CreateUserRequest(telegram_id=99999, name="Jane", email="jane@test.com")
        await uc.execute(req)

        assert len(notify.sent) == 1
        tg_id, text = notify.sent[0]
        assert tg_id == 99999
        assert "jane@test.com" in text
        assert "Jane" in text

    async def test_raises_if_user_already_exists(self) -> None:
        chatwoot = InMemoryChatwootAdminPort(next_id=400)
        user_repo = InMemoryUserProfileRepository()
        notify = InMemoryTelegramNotificationPort()
        await user_repo.save(_make_user(telegram_id=555))

        uc = CreateUserDirectUseCase(chatwoot, user_repo, notify, account_id=2)
        req = CreateUserRequest(telegram_id=555, name="X", email="x@x.com")
        with pytest.raises(ValueError, match="already exists"):
            await uc.execute(req)

    async def test_chatwoot_error_propagates(self) -> None:
        class FailingChatwoot(ChatwootAdminPort):
            async def create_agent(self, name: str, email: str, role: str) -> int:
                raise RuntimeError("Chatwoot unavailable")

            async def delete_agent(self, chatwoot_user_id: int) -> None:
                pass

        notify = InMemoryTelegramNotificationPort()
        uc = CreateUserDirectUseCase(
            FailingChatwoot(), InMemoryUserProfileRepository(), notify, account_id=2
        )
        req = CreateUserRequest(telegram_id=111, name="X", email="x@x.com")
        with pytest.raises(RuntimeError, match="Chatwoot unavailable"):
            await uc.execute(req)

    async def test_notification_not_sent_on_chatwoot_error(self) -> None:
        class FailingChatwoot(ChatwootAdminPort):
            async def create_agent(self, name: str, email: str, role: str) -> int:
                raise RuntimeError("Chatwoot unavailable")

            async def delete_agent(self, chatwoot_user_id: int) -> None:
                pass

        notify = InMemoryTelegramNotificationPort()
        uc = CreateUserDirectUseCase(
            FailingChatwoot(), InMemoryUserProfileRepository(), notify, account_id=2
        )
        req = CreateUserRequest(telegram_id=222, name="Y", email="y@y.com")
        with pytest.raises(RuntimeError):
            await uc.execute(req)
        assert len(notify.sent) == 0


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

    async def test_update_phone_internal(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        await user_repo.save(_make_user(telegram_id=1))

        uc = UpdateUserUseCase(user_repo)
        result = await uc.execute(1, UpdateUserRequest(phone_internal="+79001234567"))

        assert result is not None
        assert result.phone_internal == "+79001234567"
        saved = await user_repo.get_by_telegram_id(1)
        assert saved is not None
        assert saved.phone_internal == "+79001234567"

    async def test_update_settings(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        await user_repo.save(_make_user(telegram_id=1))

        uc = UpdateUserUseCase(user_repo)
        result = await uc.execute(1, UpdateUserRequest(settings={"lang": "ru"}))

        assert result is not None
        assert result.settings == {"lang": "ru"}

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
        chatwoot = InMemoryChatwootAdminPort()

        uc = DeactivateUserUseCase(user_repo, chatwoot)
        found = await uc.execute(1)

        assert found is True
        saved = await user_repo.get_by_telegram_id(1)
        assert saved is not None
        assert saved.is_active is False

    async def test_returns_false_for_missing_user(self) -> None:
        chatwoot = InMemoryChatwootAdminPort()
        uc = DeactivateUserUseCase(InMemoryUserProfileRepository(), chatwoot)
        assert await uc.execute(999) is False

    async def test_deactivates_without_chatwoot_call(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        user = _make_user(is_active=True)
        await user_repo.save(user)
        chatwoot = InMemoryChatwootAdminPort()

        uc = DeactivateUserUseCase(user_repo, chatwoot)
        await uc.execute(1)

        assert chatwoot.deleted_agent_ids == []

    async def test_user_is_deactivated(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        user = _make_user(is_active=True)
        await user_repo.save(user)
        chatwoot = InMemoryChatwootAdminPort()

        uc = DeactivateUserUseCase(user_repo, chatwoot)
        found = await uc.execute(1)
        assert found is True

        saved = await user_repo.get_by_telegram_id(1)
        assert saved is not None
        assert saved.is_active is False

    async def test_chatwoot_delete_not_called_for_missing_user(self) -> None:
        chatwoot = InMemoryChatwootAdminPort()
        uc = DeactivateUserUseCase(InMemoryUserProfileRepository(), chatwoot)
        await uc.execute(999)
        assert chatwoot.deleted_agent_ids == []


# ---------------------------------------------------------------------------
# Tests: DeleteUserUseCase
# ---------------------------------------------------------------------------


class TestDeleteUserUseCase:
    async def test_hard_deletes_user_from_repo(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        user = _make_user(telegram_id=1)
        await user_repo.save(user)
        chatwoot = InMemoryChatwootAdminPort()

        uc = DeleteUserUseCase(user_repo, chatwoot)
        found = await uc.execute(1)

        assert found is True
        assert await user_repo.get_by_telegram_id(1) is None

    async def test_returns_false_for_missing_user(self) -> None:
        chatwoot = InMemoryChatwootAdminPort()
        uc = DeleteUserUseCase(InMemoryUserProfileRepository(), chatwoot)
        assert await uc.execute(999) is False

    async def test_does_not_call_chatwoot(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        user = _make_user(telegram_id=1)
        await user_repo.save(user)
        chatwoot = InMemoryChatwootAdminPort()

        uc = DeleteUserUseCase(user_repo, chatwoot)
        await uc.execute(1)

        assert chatwoot.deleted_agent_ids == []

    async def test_inactive_user_can_be_hard_deleted(self) -> None:
        """Деактивированный юзер (невидимый в таблице) должен удаляться."""
        user_repo = InMemoryUserProfileRepository()
        user = _make_user(telegram_id=764347890, is_active=False)
        await user_repo.save(user)
        chatwoot = InMemoryChatwootAdminPort()

        uc = DeleteUserUseCase(user_repo, chatwoot)
        found = await uc.execute(764347890)

        assert found is True
        assert await user_repo.get_by_telegram_id(764347890) is None

    async def test_chatwoot_delete_not_called_for_missing_user(self) -> None:
        chatwoot = InMemoryChatwootAdminPort()
        uc = DeleteUserUseCase(InMemoryUserProfileRepository(), chatwoot)
        await uc.execute(999)
        assert chatwoot.deleted_agent_ids == []


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


# ---------------------------------------------------------------------------
# Helpers for Telegram auth tests
# ---------------------------------------------------------------------------


_BOT_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh"


def _make_tg_request(
    telegram_id: int = 42,
    bot_token: str = _BOT_TOKEN,
    auth_date: int | None = None,
    extra_fields: dict[str, str] | None = None,
) -> TelegramAuthRequest:
    """Build a TelegramAuthRequest with a valid HMAC hash for the given bot_token."""
    if auth_date is None:
        auth_date = int(time.time())
    data: dict[str, str] = {
        "id": str(telegram_id),
        "first_name": "Admin",
        "auth_date": str(auth_date),
    }
    if extra_fields:
        data.update({k: str(v) for k, v in extra_fields.items()})
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    hash_val = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return TelegramAuthRequest(
        id=telegram_id,
        first_name="Admin",
        auth_date=auth_date,
        hash=hash_val,
        username=extra_fields.get("username") if extra_fields else None,
    )


# ---------------------------------------------------------------------------
# Tests: verify_telegram_hash
# ---------------------------------------------------------------------------


class TestVerifyTelegramHash:
    def test_valid_hash_returns_true(self) -> None:
        req = _make_tg_request()
        data: dict[str, str | int] = {
            "id": req.id,
            "first_name": req.first_name,
            "auth_date": req.auth_date,
            "hash": req.hash,
        }
        assert verify_telegram_hash(data, _BOT_TOKEN) is True

    def test_wrong_hash_returns_false(self) -> None:
        req = _make_tg_request()
        data: dict[str, str | int] = {
            "id": req.id,
            "first_name": req.first_name,
            "auth_date": req.auth_date,
            "hash": "deadbeef" * 8,
        }
        assert verify_telegram_hash(data, _BOT_TOKEN) is False

    def test_wrong_bot_token_returns_false(self) -> None:
        req = _make_tg_request()
        data: dict[str, str | int] = {
            "id": req.id,
            "first_name": req.first_name,
            "auth_date": req.auth_date,
            "hash": req.hash,
        }
        assert verify_telegram_hash(data, "wrong:token") is False


# ---------------------------------------------------------------------------
# Tests: LoginWithTelegramUseCase
# ---------------------------------------------------------------------------


class TestLoginWithTelegramUseCase:
    _JWT_SECRET = "test-jwt-secret-32-bytes-long!!"

    async def test_valid_admin_returns_token(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        admin = _make_user(telegram_id=42, role=UserRole.ADMIN)
        await user_repo.save(admin)

        req = _make_tg_request(telegram_id=42)
        uc = LoginWithTelegramUseCase(user_repo, self._JWT_SECRET, _BOT_TOKEN)
        token = await uc.execute(req)

        assert isinstance(token, str)
        assert len(token) > 20

    async def test_valid_supervisor_returns_token(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        supervisor = _make_user(telegram_id=55, role=UserRole.SUPERVISOR)
        await user_repo.save(supervisor)

        req = _make_tg_request(telegram_id=55)
        uc = LoginWithTelegramUseCase(user_repo, self._JWT_SECRET, _BOT_TOKEN)
        token = await uc.execute(req)

        assert isinstance(token, str)

    async def test_invalid_hash_raises(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        req = TelegramAuthRequest(id=42, first_name="X", auth_date=int(time.time()), hash="invalid")
        uc = LoginWithTelegramUseCase(user_repo, self._JWT_SECRET, _BOT_TOKEN)
        with pytest.raises(ValueError, match="signature"):
            await uc.execute(req)

    async def test_expired_auth_date_raises(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        old_date = int(time.time()) - 90000  # > 24h ago
        req = _make_tg_request(telegram_id=42, auth_date=old_date)
        uc = LoginWithTelegramUseCase(user_repo, self._JWT_SECRET, _BOT_TOKEN)
        with pytest.raises(ValueError, match="expired"):
            await uc.execute(req)

    async def test_user_not_found_raises(self) -> None:
        req = _make_tg_request(telegram_id=999)
        uc = LoginWithTelegramUseCase(InMemoryUserProfileRepository(), self._JWT_SECRET, _BOT_TOKEN)
        with pytest.raises(ValueError, match="not found"):
            await uc.execute(req)

    async def test_agent_role_raises(self) -> None:
        user_repo = InMemoryUserProfileRepository()
        agent = _make_user(telegram_id=77, role=UserRole.AGENT)
        await user_repo.save(agent)

        req = _make_tg_request(telegram_id=77)
        uc = LoginWithTelegramUseCase(user_repo, self._JWT_SECRET, _BOT_TOKEN)
        with pytest.raises(ValueError, match="permissions"):
            await uc.execute(req)
