"""Tests for TelegramIngestion domain models."""
import pytest

from ..domain.models import (
    AIResult,
    ContentBlock,
    DraftSession,
    SessionStatus,
    SourceType,
)


def make_session(user_id: int = 123) -> DraftSession:
    return DraftSession(user_id=user_id)


def make_content_block(text: str = "Тестовый текст") -> ContentBlock:
    return ContentBlock(type="text", content=text)


class TestDraftSession:
    def test_initial_status_is_collecting(self) -> None:
        session = make_session()
        assert session.status == SessionStatus.COLLECTING

    def test_add_content_block_succeeds_in_collecting(self) -> None:
        session = make_session()
        block = make_content_block("Проблема с оплатой")
        session.add_content_block(block)
        assert len(session.content_blocks) == 1
        assert session.content_blocks[0].content == "Проблема с оплатой"

    def test_add_content_block_fails_in_analyzing(self) -> None:
        session = make_session()
        session.add_content_block(make_content_block("some text"))
        session.start_analysis()
        with pytest.raises(ValueError, match="Cannot add content"):
            session.add_content_block(make_content_block("more text"))

    def test_assemble_text_joins_blocks(self) -> None:
        session = make_session()
        session.add_content_block(make_content_block("Первая часть"))
        session.add_content_block(make_content_block("Вторая часть"))
        text = session.assemble_text()
        assert "Первая часть" in text
        assert "Вторая часть" in text

    def test_start_analysis_transitions_status(self) -> None:
        session = make_session()
        session.add_content_block(make_content_block("test"))
        session.start_analysis()
        assert session.status == SessionStatus.ANALYZING
        assert session.assembled_text is not None

    def test_complete_analysis_sets_result(self) -> None:
        session = make_session()
        session.add_content_block(make_content_block("test"))
        session.start_analysis()

        result = AIResult(
            title="Test Task",
            description="Test description",
            category="bug",
            priority="high",
        )
        session.complete_analysis(result)

        assert session.status == SessionStatus.PREVIEW
        assert session.ai_result is not None
        assert session.ai_result.title == "Test Task"

    def test_complete_analysis_fails_if_not_analyzing(self) -> None:
        session = make_session()
        result = AIResult(
            title="t", description="d", category="bug", priority="low"
        )
        with pytest.raises(ValueError, match="Cannot complete analysis"):
            session.complete_analysis(result)

    def test_start_editing_from_preview(self) -> None:
        session = make_session()
        session.add_content_block(make_content_block("test"))
        session.start_analysis()
        result = AIResult(title="t", description="d", category="bug", priority="low")
        session.complete_analysis(result)
        session.start_editing()
        assert session.status == SessionStatus.EDITING

    def test_add_content_block_succeeds_in_editing(self) -> None:
        session = make_session()
        session.add_content_block(make_content_block("test"))
        session.start_analysis()
        result = AIResult(title="t", description="d", category="bug", priority="low")
        session.complete_analysis(result)
        session.start_editing()
        session.add_content_block(make_content_block("дополнение"))
        assert len(session.content_blocks) == 2

    def test_default_source_type_is_manual(self) -> None:
        session = make_session()
        assert session.source_type == SourceType.MANUAL
