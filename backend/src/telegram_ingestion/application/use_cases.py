"""Telegram Ingestion — Application Use Cases."""

from __future__ import annotations

import uuid

from ..domain.models import AIResult, ContentBlock, DraftSession
from ..domain.repository import DraftSessionRepository
from .ports import STTPort, UserProfilePort


class StartSessionUseCase:
    """Create a new draft session for an authorized user."""

    def __init__(self, repo: DraftSessionRepository, user_port: UserProfilePort) -> None:
        self._repo = repo
        self._user_port = user_port

    async def execute(self, telegram_id: int) -> DraftSession | None:
        if not await self._user_port.is_authorized(telegram_id):
            return None
        existing = await self._repo.get_active_by_user(telegram_id)
        if existing is not None:
            await self._repo.delete(existing.session_id)
        session = DraftSession(user_id=telegram_id)
        await self._repo.save(session)
        return session


class AddTextContentUseCase:
    """Add a text content block to the active session."""

    def __init__(self, repo: DraftSessionRepository) -> None:
        self._repo = repo

    async def execute(self, telegram_id: int, text: str) -> DraftSession | None:
        session = await self._repo.get_active_by_user(telegram_id)
        if session is None:
            return None
        session.add_content_block(ContentBlock(type="text", content=text))
        await self._repo.save(session)
        return session


class AddVoiceContentUseCase:
    """Transcribe a voice message and add it to the active session."""

    def __init__(self, repo: DraftSessionRepository, stt_port: STTPort) -> None:
        self._repo = repo
        self._stt_port = stt_port

    async def execute(
        self, telegram_id: int, file_id: str, file_bytes: bytes
    ) -> DraftSession | None:
        session = await self._repo.get_active_by_user(telegram_id)
        if session is None:
            return None
        transcription = await self._stt_port.transcribe(file_bytes)
        block = ContentBlock(type="voice", content=transcription, file_id=file_id)
        session.add_content_block(block)
        await self._repo.save(session)
        return session


class TriggerAnalysisUseCase:
    """Transition active session from COLLECTING to ANALYZING."""

    def __init__(self, repo: DraftSessionRepository) -> None:
        self._repo = repo

    async def execute(self, telegram_id: int) -> DraftSession | None:
        session = await self._repo.get_active_by_user(telegram_id)
        if session is None:
            return None
        session.start_analysis()
        await self._repo.save(session)
        return session


class SetAnalysisResultUseCase:
    """Set AI result on session, transitioning from ANALYZING to PREVIEW."""

    def __init__(self, repo: DraftSessionRepository) -> None:
        self._repo = repo

    async def execute(self, session_id: uuid.UUID, result: AIResult) -> DraftSession | None:
        session = await self._repo.get_by_id(session_id)
        if session is None:
            return None
        session.complete_analysis(result)
        await self._repo.save(session)
        return session


class CancelSessionUseCase:
    """Cancel and delete the active session."""

    def __init__(self, repo: DraftSessionRepository) -> None:
        self._repo = repo

    async def execute(self, telegram_id: int) -> bool:
        session = await self._repo.get_active_by_user(telegram_id)
        if session is None:
            return False
        await self._repo.delete(session.session_id)
        return True
