"""AI Classification — Abstract Repository and Port."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import ClassificationResult


class AIClassificationPort(ABC):
    """Anti-Corruption Layer: интерфейс к OpenRouter API."""

    @abstractmethod
    async def classify(self, text: str) -> ClassificationResult: ...
