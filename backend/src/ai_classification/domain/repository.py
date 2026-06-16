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

    @abstractmethod
    async def extract_location_name(
        self, dialogue_text: str, known_names: list[str]
    ) -> str | None:
        """Return one displayName from known_names that matches the dialogue, or None.

        AI MUST pick from the catalog — it must NOT invent new names. Whisper
        often mangles brand names (`Поло`, `Алола` → Аполо), so the prompt
        normalizes before matching.
        """
        ...

    # Note: `classify_call_intent`, `check_repeat_status`, and
    # `check_script` are implemented on OpenRouterAdapter via runtime
    # attachment (see infrastructure/openrouter_adapter.py). They are
    # intentionally NOT @abstractmethod here — declaring them so would
    # break adapter instantiation. Use cases consume these via Protocol
    # types in their own modules.
