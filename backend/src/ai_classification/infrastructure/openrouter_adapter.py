"""AI Classification — OpenRouter Adapter (AIClassificationPort implementation)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..domain.models import (
    Category,
    ClassificationEntities,
    ClassificationResult,
    Priority,
    TaskFieldSelection,
)
from ..domain.repository import AIClassificationPort

logger = logging.getLogger(__name__)

CLASSIFICATION_PROMPT = """\
Ты — классификатор обращений в службу технической поддержки 24ondoc.
Компания занимается обслуживанием торгового оборудования, кассовых аппаратов, \
программ 1С, ЕГАИС, ЭДО, сканеров, ОФД и фискальных накопителей.

Проанализируй входящее обращение и верни JSON-объект со следующей структурой:
{
    "title": "<краткий заголовок на русском>",
    "description": "<развёрнутое описание проблемы или запроса>",
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
        primary_model: str = "anthropic/claude-sonnet-4.6",
        fallback_model: str = "openrouter/free",
    ) -> None:
        self._api_key = api_key
        self._primary_model = primary_model
        self._fallback_model = fallback_model
        self._circuit_breaker = _CircuitBreaker()

    async def classify(self, text: str) -> ClassificationResult:
        if self._circuit_breaker.is_open():
            raise CircuitBreakerOpenError("OpenRouter circuit breaker is open")

        try:
            primary_result: ClassificationResult = await self._classify_with_retry(
                text,
                self._primary_model,
            )
            self._circuit_breaker.record_success()
            return primary_result
        except Exception:
            self._circuit_breaker.record_failure()
            fallback_result: ClassificationResult = await self._classify_with_retry(
                text,
                self._fallback_model,
            )
            return fallback_result

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, json.JSONDecodeError, ValueError)),
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
        if not content:
            raise ValueError(f"Empty content from model {model}")
        # Strip markdown code fences if present
        text_to_parse = content.strip()
        if text_to_parse.startswith("```"):
            text_to_parse = text_to_parse.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(text_to_parse)

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

    TASK_FIELDS_PROMPT = """\
Ты — помощник системы 24ondoc. Проанализируй текст обращения \
и выбери наиболее подходящие значения из предложенных списков.

Доступные категории (kategoriya):
{kategoriya_list}

Доступные уровни важности (vazhnost):
{vazhnost_list}

Правила:
- Если ни одна категория не подходит — верни null для kategoriya.
- Если не можешь определить важность — верни null для vazhnost.
- Возвращай ТОЛЬКО value из списка, не label.

Ответь ТОЛЬКО JSON-объектом:
{{"kategoriya": "<value или null>", "vazhnost": "<value или null>"}}"""

    async def select_task_fields(
        self,
        text: str,
        kategoriya_options: list[dict[str, str]],
        vazhnost_options: list[dict[str, str]],
    ) -> TaskFieldSelection:
        """Use AI to select best kategoriya and vazhnost from provided options."""
        kat_list = (
            "\n".join(f'- value="{o["value"]}", label="{o["label"]}"' for o in kategoriya_options)
            or "(список пуст)"
        )
        vazh_list = (
            "\n".join(f'- value="{o["value"]}", label="{o["label"]}"' for o in vazhnost_options)
            or "(список пуст)"
        )

        prompt = self.TASK_FIELDS_PROMPT.format(kategoriya_list=kat_list, vazhnost_list=vazh_list)

        valid_kat = {o["value"] for o in kategoriya_options}
        valid_vazh = {o["value"] for o in vazhnost_options}

        for model in (self._primary_model, self._fallback_model):
            try:
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
                                {"role": "system", "content": prompt},
                                {"role": "user", "content": text},
                            ],
                            "response_format": {"type": "json_object"},
                        },
                    )
                    response.raise_for_status()

                data = response.json()
                content = data["choices"][0]["message"]["content"]
                if not content:
                    raise ValueError(f"Empty content from model {model}")
                text_to_parse = content.strip()
                if text_to_parse.startswith("```"):
                    text_to_parse = text_to_parse.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                parsed = json.loads(text_to_parse)

                kat_value = parsed.get("kategoriya")
                vazh_value = parsed.get("vazhnost")

                result = TaskFieldSelection(
                    kategoriya=kat_value if kat_value in valid_kat else None,
                    vazhnost=vazh_value if vazh_value in valid_vazh else None,
                )
                logger.info(
                    "select_task_fields OK (model=%s): kat=%s, vazh=%s",
                    model,
                    result.kategoriya,
                    result.vazhnost,
                )
                return result
            except Exception:
                logger.warning("select_task_fields failed with model=%s", model, exc_info=True)

        logger.error("select_task_fields: all models failed, returning defaults")
        return TaskFieldSelection()
