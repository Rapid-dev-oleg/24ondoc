"""Admin panel — Abstract ports (interfaces to external services)."""
from __future__ import annotations

from abc import ABC, abstractmethod


class ChatwootAdminPort(ABC):
    """Port for creating agents in Chatwoot via admin API."""

    @abstractmethod
    async def create_agent(self, name: str, email: str, role: str) -> int:
        """Create an agent in Chatwoot, return chatwoot_user_id."""
        ...


class EnvSettingsPort(ABC):
    """Port for reading/writing environment variable settings."""

    @abstractmethod
    def get_setting(self, key: str) -> str | None:
        """Return the current value of the env var, or None if not set."""
        ...

    @abstractmethod
    def update_setting(self, key: str, value: str) -> None:
        """Write a new value for the env var to the backing store."""
        ...
