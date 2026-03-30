"""Telegram Ingestion — Phone Authorization Use Cases."""

from __future__ import annotations

from ..domain.models import PendingUser, UserProfile, UserRole
from ..domain.repository import PendingUserRepository, UserProfileRepository
from .ports import UserProfilePort


def normalize_phone(raw: str) -> str:
    """Нормализует телефон: только цифры, 8→7 для российских номеров."""
    digits = "".join(c for c in raw if c.isdigit())
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    return digits


class AuthByPhoneUseCase:
    """Авторизация пользователя Telegram по номеру телефона через pending_users."""

    def __init__(
        self,
        pending_repo: PendingUserRepository,
        user_repo: UserProfileRepository,
    ) -> None:
        self._pending_repo = pending_repo
        self._user_repo = user_repo

    async def execute(self, telegram_id: int, phone: str) -> UserProfile | None:
        normalized = normalize_phone(phone)
        pending = await self._pending_repo.get_by_phone(normalized)
        if pending is None:
            return None
        profile = UserProfile(
            telegram_id=telegram_id,
            role=pending.role,
        )
        await self._user_repo.save(profile)
        await self._pending_repo.delete(normalized)
        return profile


class RegisterPhoneUseCase:
    """Регистрация телефона в pending_users (только для admin/supervisor)."""

    def __init__(
        self,
        pending_repo: PendingUserRepository,
        user_port: UserProfilePort,
    ) -> None:
        self._pending_repo = pending_repo
        self._user_port = user_port

    async def execute(
        self,
        requester_telegram_id: int,
        phone: str,
        chatwoot_user_id: int,
        chatwoot_account_id: int,
        role: UserRole = UserRole.AGENT,
    ) -> bool:
        requester = await self._user_port.get_profile(requester_telegram_id)
        if requester is None or requester.role not in (UserRole.ADMIN, UserRole.SUPERVISOR):
            return False
        normalized = normalize_phone(phone)
        pending = PendingUser(
            phone=normalized,
            chatwoot_user_id=chatwoot_user_id,
            chatwoot_account_id=chatwoot_account_id,
            role=role,
        )
        await self._pending_repo.save(pending)
        return True
