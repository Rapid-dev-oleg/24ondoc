"""ATS Processing — SQLAlchemy CallRecord Repository."""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.models import CallRecord, CallStatus, SourceType
from ..domain.repository import CallRecordRepository
from .orm_models import CallRecordORM


class CallRecordRepositoryImpl(CallRecordRepository):
    """SQLAlchemy implementation of CallRecordRepository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, call_id: str) -> CallRecord | None:
        row = await self._session.get(CallRecordORM, call_id)
        return self._to_domain(row) if row is not None else None

    async def save(self, record: CallRecord) -> None:
        row = await self._session.get(CallRecordORM, record.call_id)
        if row is None:
            self._session.add(self._to_orm(record))
        else:
            self._update_orm(row, record)
        await self._session.flush()

    async def get_pending(
        self, limit: int = 10, source: SourceType | None = None
    ) -> list[CallRecord]:
        query = select(CallRecordORM).where(CallRecordORM.status == CallStatus.NEW.value)
        if source is not None:
            query = query.where(CallRecordORM.source == source.value)
        query = query.order_by(CallRecordORM.created_at.asc()).limit(limit)
        result = await self._session.execute(query)
        return [self._to_domain(row) for row in result.scalars()]

    async def find_recent_by_phone(self, phone: str, limit: int = 10) -> list[CallRecord]:
        result = await self._session.execute(
            select(CallRecordORM)
            .where(CallRecordORM.caller_phone == phone)
            .order_by(CallRecordORM.created_at.desc())
            .limit(limit)
        )
        return [self._to_domain(row) for row in result.scalars()]

    async def get_recent(self, limit: int = 10) -> list[CallRecord]:
        result = await self._session.execute(
            select(CallRecordORM).order_by(CallRecordORM.created_at.desc()).limit(limit)
        )
        return [self._to_domain(row) for row in result.scalars()]

    async def set_twenty_task_by_session(
        self, session_id: uuid.UUID, twenty_task_id: str
    ) -> bool:
        result = await self._session.execute(
            update(CallRecordORM)
            .where(CallRecordORM.session_id == session_id)
            .values(twenty_task_id=twenty_task_id)
        )
        # execute(update(...)) returns CursorResult; Result stub hides rowcount.
        return int(getattr(result, "rowcount", 0) or 0) > 0

    @staticmethod
    def _to_domain(row: CallRecordORM) -> CallRecord:
        return CallRecord(
            call_id=row.call_id,
            audio_url=row.audio_url,
            source=SourceType(row.source),
            transcription_t2=row.transcription_t2,
            transcription_whisper=row.transcription_whisper,
            duration=row.duration,
            caller_phone=row.caller_phone,
            agent_ext=row.agent_ext,
            detected_agent_id=row.detected_agent_id,
            voice_match_score=row.voice_match_score,
            status=CallStatus(row.status),
            session_id=row.session_id,
            twenty_task_id=row.twenty_task_id,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_orm(record: CallRecord) -> CallRecordORM:
        return CallRecordORM(
            call_id=record.call_id,
            audio_url=record.audio_url,
            source=record.source.value,
            transcription_t2=record.transcription_t2,
            transcription_whisper=record.transcription_whisper,
            duration=record.duration,
            caller_phone=record.caller_phone,
            agent_ext=record.agent_ext,
            detected_agent_id=record.detected_agent_id,
            voice_match_score=record.voice_match_score,
            status=record.status.value,
            session_id=record.session_id,
            twenty_task_id=record.twenty_task_id,
            created_at=record.created_at,
        )

    @staticmethod
    def _update_orm(row: CallRecordORM, record: CallRecord) -> None:
        row.audio_url = record.audio_url
        row.source = record.source.value
        row.transcription_t2 = record.transcription_t2
        row.transcription_whisper = record.transcription_whisper
        row.duration = record.duration
        row.caller_phone = record.caller_phone
        row.agent_ext = record.agent_ext
        row.detected_agent_id = record.detected_agent_id
        row.voice_match_score = record.voice_match_score
        row.status = record.status.value
        row.session_id = record.session_id
        row.twenty_task_id = record.twenty_task_id
