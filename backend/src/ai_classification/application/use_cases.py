"""AI Classification — Application Use Cases."""

from __future__ import annotations

from ..domain.models import ClassificationResult
from ..domain.repository import AIClassificationPort


class ClassifyRequest:
    """Классифицирует текстовое обращение через AI-порт."""

    def __init__(self, port: AIClassificationPort) -> None:
        self._port = port

    async def execute(self, text: str) -> ClassificationResult:
        return await self._port.classify(text)
