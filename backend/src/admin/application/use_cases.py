"""Admin panel — Application use cases."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from admin.application.ports import ChatwootAdminPort, EnvSettingsPort, TelegramNotificationPort
from admin.domain.models import (
    CreateUserRequest,
    SettingsResponse,
    TelegramAuthRequest,
    UpdateSettingsRequest,
    UpdateUserRequest,
    UserResponse,
)
from telegram_ingestion.domain.models import UserProfile, UserRole
from telegram_ingestion.domain.repository import UserProfileRepository


def _mask_value(value: str) -> str:
    """Mask a sensitive value, exposing only the last 4 characters."""
    if len(value) <= 4:
        return "****"
    return "*" * (len(value) - 4) + value[-4:]


class ListUsersUseCase:
    """Return all active users."""

    def __init__(self, user_repo: UserProfileRepository) -> None:
        self._users = user_repo

    async def execute(self) -> list[UserResponse]:
        users = await self._users.list_active()
        return [
            UserResponse(
                telegram_id=u.telegram_id,
                chatwoot_user_id=u.chatwoot_user_id,
                chatwoot_account_id=u.chatwoot_account_id,
                role=u.role,
                is_active=u.is_active,
                is_pending=False,
                created_at=u.created_at,
            )
            for u in users
        ]


class CreateUserDirectUseCase:
    """Create user in Chatwoot + users table directly, then notify via Telegram."""

    def __init__(
        self,
        chatwoot: ChatwootAdminPort,
        user_repo: UserProfileRepository,
        notify: TelegramNotificationPort,
        account_id: int,
    ) -> None:
        self._chatwoot = chatwoot
        self._users = user_repo
        self._notify = notify
        self._account_id = account_id

    async def execute(self, request: CreateUserRequest) -> UserResponse:
        existing = await self._users.get_by_telegram_id(request.telegram_id)
        if existing is not None:
            raise ValueError(f"User {request.telegram_id} already exists")

        chatwoot_user_id = await self._chatwoot.create_agent(
            name=request.name,
            email=request.email,
            role=request.role.value,
        )

        profile = UserProfile(
            telegram_id=request.telegram_id,
            chatwoot_user_id=chatwoot_user_id,
            chatwoot_account_id=self._account_id,
            role=request.role,
        )
        await self._users.save(profile)

        password = secrets.token_urlsafe(12)
        text = (
            "✅ Вы зарегистрированы в системе 24ondoc!\n\n"
            f"Имя: {request.name}\n"
            f"Email: {request.email}\n"
            f"Роль: {request.role.value}\n\n"
            f"Временный пароль Chatwoot: {password}\n"
            "На email придёт приглашение для активации аккаунта.\n\n"
            "Используйте /new_task в боте для создания задач."
        )
        await self._notify.send_message(request.telegram_id, text)

        return UserResponse(
            telegram_id=profile.telegram_id,
            chatwoot_user_id=profile.chatwoot_user_id,
            chatwoot_account_id=profile.chatwoot_account_id,
            role=profile.role,
            is_active=profile.is_active,
            is_pending=False,
            created_at=profile.created_at,
        )


class UpdateUserUseCase:
    """Update role and/or is_active for an existing user."""

    def __init__(self, user_repo: UserProfileRepository) -> None:
        self._users = user_repo

    async def execute(self, telegram_id: int, request: UpdateUserRequest) -> UserResponse | None:
        user = await self._users.get_by_telegram_id(telegram_id)
        if user is None:
            return None
        updates: dict[str, object] = {}
        if request.role is not None:
            updates["role"] = request.role
        if request.is_active is not None:
            updates["is_active"] = request.is_active
        if updates:
            user = user.model_copy(update=updates)
            await self._users.save(user)
        return UserResponse(
            telegram_id=user.telegram_id,
            chatwoot_user_id=user.chatwoot_user_id,
            chatwoot_account_id=user.chatwoot_account_id,
            role=user.role,
            is_active=user.is_active,
            is_pending=False,
            created_at=user.created_at,
        )


class DeactivateUserUseCase:
    """Set is_active=False for a user (soft delete) and remove agent from Chatwoot CRM."""

    def __init__(self, user_repo: UserProfileRepository, chatwoot: ChatwootAdminPort) -> None:
        self._users = user_repo
        self._chatwoot = chatwoot

    async def execute(self, telegram_id: int) -> bool:
        user = await self._users.get_by_telegram_id(telegram_id)
        if user is None:
            return False
        user = user.model_copy(update={"is_active": False})
        await self._users.save(user)
        await self._chatwoot.delete_agent(user.chatwoot_user_id)
        return True


class GetSettingsUseCase:
    """Return masked environment settings."""

    def __init__(self, env_port: EnvSettingsPort) -> None:
        self._env = env_port

    def execute(self) -> SettingsResponse:
        openrouter_key = self._env.get_setting("OPENROUTER_API_KEY") or ""
        telegram_token = self._env.get_setting("TELEGRAM_BOT_TOKEN") or ""
        return SettingsResponse(
            openrouter_api_key=_mask_value(openrouter_key),
            telegram_bot_token=_mask_value(telegram_token),
        )


class UpdateSettingsUseCase:
    """Write new values to .env and return masked view."""

    def __init__(self, env_port: EnvSettingsPort) -> None:
        self._env = env_port

    def execute(self, request: UpdateSettingsRequest) -> SettingsResponse:
        if request.openrouter_api_key is not None:
            self._env.update_setting("OPENROUTER_API_KEY", request.openrouter_api_key)
        if request.telegram_bot_token is not None:
            self._env.update_setting("TELEGRAM_BOT_TOKEN", request.telegram_bot_token)
        return GetSettingsUseCase(self._env).execute()


_AUTH_DATE_MAX_AGE_SECONDS = 86400  # 24 hours


def verify_telegram_hash(data: dict[str, str | int], bot_token: str) -> bool:
    """Verify the HMAC-SHA256 signature in Telegram Login Widget callback data."""
    check_hash = str(data.get("hash", ""))
    pairs = {k: str(v) for k, v in data.items() if k != "hash"}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, check_hash)


class LoginWithTelegramUseCase:
    """Verify Telegram Login Widget auth data and issue a JWT."""

    def __init__(self, user_repo: UserProfileRepository, jwt_secret: str, bot_token: str) -> None:
        self._users = user_repo
        self._jwt_secret = jwt_secret
        self._bot_token = bot_token

    async def execute(self, request: TelegramAuthRequest) -> str:
        """Return a JWT access token or raise ValueError on invalid auth."""
        data: dict[str, str | int] = {
            "id": request.id,
            "first_name": request.first_name,
            "auth_date": request.auth_date,
            "hash": request.hash,
        }
        if request.last_name is not None:
            data["last_name"] = request.last_name
        if request.username is not None:
            data["username"] = request.username
        if request.photo_url is not None:
            data["photo_url"] = request.photo_url

        if not verify_telegram_hash(data, self._bot_token):
            raise ValueError("Invalid Telegram auth signature")

        if time.time() - request.auth_date > _AUTH_DATE_MAX_AGE_SECONDS:
            raise ValueError("Telegram auth data expired")

        user = await self._users.get_by_telegram_id(request.id)
        if user is None or not user.is_active:
            raise ValueError("User not found or inactive")
        if user.role.value not in (UserRole.ADMIN.value, UserRole.SUPERVISOR.value):
            raise ValueError("Insufficient permissions")

        from admin.infrastructure.auth import create_access_token  # avoid circular import

        token: str = create_access_token(user.telegram_id, user.role.value, self._jwt_secret)
        return token
