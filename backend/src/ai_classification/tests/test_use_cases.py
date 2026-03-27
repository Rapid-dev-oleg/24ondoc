"""Tests for AI Classification use cases."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ..application.use_cases import ClassifyRequest
from ..domain.models import Category, ClassificationResult, Priority
from ..domain.repository import AIClassificationPort


@pytest.fixture()
def mock_port() -> AIClassificationPort:
    port = AsyncMock(spec=AIClassificationPort)
    port.classify.return_value = ClassificationResult(
        source_text="Нет воды в кране",
        title="Проблема с водоснабжением",
        description="Клиент сообщает об отсутствии воды",
        category=Category.COMPLAINT,
        priority=Priority.HIGH,
        model_used="anthropic/claude-3.5-sonnet",
    )
    return port


@pytest.mark.asyncio
async def test_classify_request_returns_classification_result(
    mock_port: AIClassificationPort,
) -> None:
    use_case = ClassifyRequest(port=mock_port)
    result = await use_case.execute("Нет воды в кране")

    assert isinstance(result, ClassificationResult)
    assert result.category == Category.COMPLAINT
    assert result.priority == Priority.HIGH
    mock_port.classify.assert_awaited_once_with("Нет воды в кране")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_classify_request_propagates_port_error(
    mock_port: AIClassificationPort,
) -> None:
    mock_port.classify.side_effect = RuntimeError("API error")  # type: ignore[attr-defined]
    use_case = ClassifyRequest(port=mock_port)

    with pytest.raises(RuntimeError, match="API error"):
        await use_case.execute("some text")


@pytest.mark.asyncio
async def test_classify_request_passes_text_verbatim(
    mock_port: AIClassificationPort,
) -> None:
    use_case = ClassifyRequest(port=mock_port)
    await use_case.execute("Тестовый запрос 123")
    mock_port.classify.assert_awaited_once_with("Тестовый запрос 123")  # type: ignore[attr-defined]
