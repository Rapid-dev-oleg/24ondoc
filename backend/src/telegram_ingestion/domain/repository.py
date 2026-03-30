"""Telegram Ingestion — Abstract Repositories."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from .models import DraftSession, PendingUser, UserProfile


class DraftSessionRepository(ABC):
    @abstractmethod
    async def get_by_id(self, session_id: uuid.UUID) -> DraftSession | None: ...

    @abstractmethod
    async def get_active_by_user(self, user_id: int) -> DraftSession | None: ...

    @abstractmethod
    async def save(self, session: DraftSession) -> None: ...

    @abstractmethod
    async def delete(self, session_id: uuid.UUID) -> None: ...


class PendingUserRepository(ABC):
    @abstractmethod
    async def get_by_phone(self, phone: str) -> PendingUser | None: ...

    @abstractmethod
    async def save(self, pending: PendingUser) -> None: ...

    @abstractmethod
    async def delete(self, phone: str) -> None: ...

    @abstractmethod
    async def list_all(self) -> list[PendingUser]: ...


class UserProfileRepository(ABC):
    @abstractmethod
    async def get_by_telegram_id(self, telegram_id: int) -> UserProfile | None: ...

    @abstractmethod
    async def save(self, profile: UserProfile) -> None: ...

    @abstractmethod
    async def list_active(self) -> list[UserProfile]: ...

    @abstractmethod
    async def list_all(self) -> list[UserProfile]: ...

    @abstractmethod
    async def delete_by_telegram_id(self, telegram_id: int) -> None: ...
