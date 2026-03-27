"""Admin panel — Application use cases."""
from __future__ import annotations

from admin.application.ports import ChatwootAdminPort, EnvSettingsPort
from admin.domain.models import (
    AddPendingRequest,
    CreateUserRequest,
    PendingUserResponse,
    SettingsResponse,
    UpdateSettingsRequest,
    UpdateUserRequest,
    UserResponse,
)
from telegram_ingestion.application.auth_use_case import normalize_phone
from telegram_ingestion.domain.models import PendingUser, UserRole
from telegram_ingestion.domain.repository import PendingUserRepository, UserProfileRepository


def _mask_value(value: str) -> str:
    """Mask a sensitive value, exposing only the last 4 characters."""
    if len(value) <= 4:
        return "****"
    return "*" * (len(value) - 4) + value[-4:]


class ListUsersUseCase:
    """Return all active users and all pending users combined."""

    def __init__(
        self,
        user_repo: UserProfileRepository,
        pending_repo: PendingUserRepository,
    ) -> None:
        self._users = user_repo
        self._pending = pending_repo

    async def execute(self) -> list[UserResponse]:
        users = await self._users.list_active()
        pending_users = await self._pending.list_all()

        result: list[UserResponse] = []
        for u in users:
            result.append(
                UserResponse(
                    telegram_id=u.telegram_id,
                    phone=u.phone_internal,
                    chatwoot_user_id=u.chatwoot_user_id,
                    chatwoot_account_id=u.chatwoot_account_id,
                    role=u.role,
                    is_active=u.is_active,
                    is_pending=False,
                    created_at=u.created_at,
                )
            )
        for p in pending_users:
            result.append(
                UserResponse(
                    telegram_id=None,
                    phone=p.phone,
                    chatwoot_user_id=p.chatwoot_user_id,
                    chatwoot_account_id=p.chatwoot_account_id,
                    role=p.role,
                    is_active=None,
                    is_pending=True,
                    created_at=p.created_at,
                )
            )
        return result


class CreateOperatorUseCase:
    """Create an operator in Chatwoot and register them in pending_users."""

    def __init__(
        self,
        chatwoot: ChatwootAdminPort,
        pending_repo: PendingUserRepository,
        account_id: int,
    ) -> None:
        self._chatwoot = chatwoot
        self._pending = pending_repo
        self._account_id = account_id

    async def execute(self, request: CreateUserRequest) -> PendingUserResponse:
        chatwoot_user_id = await self._chatwoot.create_agent(
            name=request.name,
            email=request.email,
            role=request.role.value,
        )
        phone = normalize_phone(request.phone)
        pending = PendingUser(
            phone=phone,
            chatwoot_user_id=chatwoot_user_id,
            chatwoot_account_id=self._account_id,
            role=request.role,
        )
        await self._pending.save(pending)
        return PendingUserResponse(
            phone=pending.phone,
            chatwoot_user_id=pending.chatwoot_user_id,
            chatwoot_account_id=pending.chatwoot_account_id,
            role=pending.role,
            created_at=pending.created_at,
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
            phone=user.phone_internal,
            chatwoot_user_id=user.chatwoot_user_id,
            chatwoot_account_id=user.chatwoot_account_id,
            role=user.role,
            is_active=user.is_active,
            is_pending=False,
            created_at=user.created_at,
        )


class DeactivateUserUseCase:
    """Set is_active=False for a user (soft delete)."""

    def __init__(self, user_repo: UserProfileRepository) -> None:
        self._users = user_repo

    async def execute(self, telegram_id: int) -> bool:
        user = await self._users.get_by_telegram_id(telegram_id)
        if user is None:
            return False
        user = user.model_copy(update={"is_active": False})
        await self._users.save(user)
        return True


class ListPendingUseCase:
    """Return all pending users."""

    def __init__(self, pending_repo: PendingUserRepository) -> None:
        self._pending = pending_repo

    async def execute(self) -> list[PendingUserResponse]:
        pending_users = await self._pending.list_all()
        return [
            PendingUserResponse(
                phone=p.phone,
                chatwoot_user_id=p.chatwoot_user_id,
                chatwoot_account_id=p.chatwoot_account_id,
                role=p.role,
                created_at=p.created_at,
            )
            for p in pending_users
        ]


class AddPendingUseCase:
    """Add a phone to pending_users directly (without Chatwoot)."""

    def __init__(self, pending_repo: PendingUserRepository) -> None:
        self._pending = pending_repo

    async def execute(self, request: AddPendingRequest) -> PendingUserResponse:
        phone = normalize_phone(request.phone)
        pending = PendingUser(
            phone=phone,
            chatwoot_user_id=request.chatwoot_user_id,
            chatwoot_account_id=request.chatwoot_account_id,
            role=request.role,
        )
        await self._pending.save(pending)
        return PendingUserResponse(
            phone=pending.phone,
            chatwoot_user_id=pending.chatwoot_user_id,
            chatwoot_account_id=pending.chatwoot_account_id,
            role=pending.role,
            created_at=pending.created_at,
        )


class DeletePendingUseCase:
    """Remove a pending user by phone."""

    def __init__(self, pending_repo: PendingUserRepository) -> None:
        self._pending = pending_repo

    async def execute(self, phone: str) -> bool:
        normalized = normalize_phone(phone)
        existing = await self._pending.get_by_phone(normalized)
        if existing is None:
            return False
        await self._pending.delete(normalized)
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
