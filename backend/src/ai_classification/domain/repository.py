"""AI Classification — Abstract Repository and Port."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import ClassificationResult, TaskFieldSelection


class AIClassificationPort(ABC):
    """Anti-Corruption Layer: интерфейс к OpenRouter API."""

    @abstractmethod
    async def classify(self, text: str) -> ClassificationResult: ...

    @abstractmethod
    async def select_task_fields(
        self,
        text: str,
        kategoriya_options: list[dict[str, str]],
        vazhnost_options: list[dict[str, str]],
    ) -> TaskFieldSelection:
        """Select best kategoriya and vazhnost values from provided options."""
        ...
