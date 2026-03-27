"""AI Classification — OpenRouter Adapter (AIClassificationPort implementation)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..domain.models import Category, ClassificationEntities, ClassificationResult, Priority
from ..domain.repository import AIClassificationPort

CLASSIFICATION_PROMPT = """\
Ты — классификатор медицинских обращений для системы 24ondoc.
Проанализируй входящее обращение и верни JSON-объект со следующей структурой:
{
    "title": "<краткий заголовок на русском>",
    "description": "<развёрнутое описание обращения>",
    "category": "<bug|feature|question|complaint|other>",
    "priority": "<low|medium|high|urgent>",
    "deadline": "<ISO дата или null>",
    "entities": {
        "emails": [],
        "phones": [],
        "prices": [],
        "dates": []
    },
    "assignee_hint": "<отдел или null>"
}
Отвечай ТОЛЬКО JSON-объектом, без дополнительного текста."""


class CircuitBreakerOpenError(Exception):
    """Raised when the circuit breaker is open and calls are rejected."""


@dataclass
class _CircuitBreaker:
    threshold: int = 5
    reset_timeout: float = 60.0
    _failure_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _open: bool = field(default=False, init=False)

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.threshold:
            self._open = True

    def record_success(self) -> None:
        self._failure_count = 0
        self._open = False

    def is_open(self) -> bool:
        if self._open:
            if time.monotonic() - self._last_failure_time >= self.reset_timeout:
                self._open = False
                self._failure_count = 0
                return False
            return True
        return False


class OpenRouterAdapter(AIClassificationPort):
    """Реализация AIClassificationPort через OpenRouter API с circuit breaker и fallback."""

    _BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        api_key: str,
        primary_model: str = "anthropic/claude-3.5-sonnet",
        fallback_model: str = "openai/gpt-4o",
    ) -> None:
        self._api_key = api_key
        self._primary_model = primary_model
        self._fallback_model = fallback_model
        self._circuit_breaker = _CircuitBreaker()

    async def classify(self, text: str) -> ClassificationResult:
        if self._circuit_breaker.is_open():
            raise CircuitBreakerOpenError("OpenRouter circuit breaker is open")

        try:
            result = await self._classify_with_retry(text, self._primary_model)
            self._circuit_breaker.record_success()
            return result
        except Exception:
            self._circuit_breaker.record_failure()
            return await self._classify_with_retry(text, self._fallback_model)

    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _classify_with_retry(self, text: str, model: str) -> ClassificationResult:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self._BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": CLASSIFICATION_PROMPT},
                        {"role": "user", "content": text},
                    ],
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)

        return ClassificationResult(
            source_text=text,
            title=parsed.get("title", ""),
            description=parsed.get("description", ""),
            category=Category(parsed.get("category", "other")),
            priority=Priority(parsed.get("priority", "medium")),
            deadline=parsed.get("deadline"),
            entities=ClassificationEntities(**parsed.get("entities", {})),
            assignee_hint=parsed.get("assignee_hint"),
            model_used=model,
        )
