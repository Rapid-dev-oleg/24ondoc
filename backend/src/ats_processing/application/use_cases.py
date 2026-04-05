"""ATS Processing — Application Use Cases."""

from __future__ import annotations

import logging
import re
import uuid
from abc import ABC, abstractmethod

from ..domain.models import CallRecord, CallStatus
from ..domain.repository import AgentVoiceSampleRepository, CallRecordRepository
from .ports import VoiceEmbeddingPort

logger = logging.getLogger(__name__)

# Regex fallback: ищет имя агента в транскрипции
_AGENT_NAME_PATTERN = re.compile(
    r"\b(меня зовут|это|говорит|агент)\s+([А-ЯЁA-Z][а-яёa-z]+(?:\s+[А-ЯЁA-Z][а-яёa-z]+)?)",
    re.IGNORECASE,
)


# Type alias (избегаем circular import)
class STTPortLike:
    async def transcribe(self, audio_bytes: bytes) -> str:  # pragma: no cover
        raise NotImplementedError


class IdentifyAgentByVoice:
    """
    Use case: идентификация агента по голосу через pgvector cosine similarity.

    Шаги:
    1. Извлечь embedding из аудио (VoiceEmbeddingPort)
    2. Cosine similarity с agent_voice_samples
    3. Если score >= threshold → set_voice_match
    4. Иначе → regex fallback по транскрипции
    """

    def __init__(
        self,
        embedding_port: VoiceEmbeddingPort,
        voice_repo: AgentVoiceSampleRepository,
        threshold: float = 0.85,
    ) -> None:
        self._embedding_port = embedding_port
        self._voice_repo = voice_repo
        self._threshold = threshold

    async def execute(self, call_record: CallRecord, audio_bytes: bytes) -> int | None:
        """Returns detected agent_id or None if unidentified."""
        embedding = await self._embedding_port.embed(audio_bytes)
        result = await self._voice_repo.find_closest(embedding)

        if result is not None:
            agent_id, score = result
            if score >= self._threshold:
                call_record.set_voice_match(agent_id, score)
                return agent_id

        # Fallback: regex в транскрипции
        transcription = call_record.get_best_transcription()
        if transcription:
            match = _AGENT_NAME_PATTERN.search(transcription)
            if match:
                logger.debug("Agent identified by regex fallback: %s", match.group(2))
                return None  # имя найдено, но agent_id неизвестен без справочника

        return None


# ============================================================
# EnrollVoiceSampleUseCase
# ============================================================


class EnrollVoiceSampleUseCase:
    """
    Use case: сохранить voice embedding агента в pgvector.

    Шаги:
    1. Извлечь embedding из аудио (VoiceEmbeddingPort)
    2. Сохранить/перезаписать embedding в AgentVoiceSampleRepository
    3. Вернуть True при успехе, False при любой ошибке
    """

    def __init__(
        self,
        embedding_port: VoiceEmbeddingPort,
        voice_repo: AgentVoiceSampleRepository,
    ) -> None:
        self._embedding_port = embedding_port
        self._voice_repo = voice_repo

    async def execute(self, agent_id: int, audio_bytes: bytes) -> bool:
        """Extract embedding and persist it. Returns True on success, False on any Exception."""
        try:
            embedding = await self._embedding_port.embed(audio_bytes)
            await self._voice_repo.save(agent_id, embedding)
            return True
        except Exception:
            logger.exception("EnrollVoiceSampleUseCase failed for agent_id %s", agent_id)
            return False


# ============================================================
# Notification Port
# ============================================================


class TelegramNotificationPort(ABC):
    """Port for sending Telegram notifications about processed calls."""

    @abstractmethod
    async def send_call_notification(
        self,
        chat_id: int,
        call_record: CallRecord,
    ) -> None:
        """Send a call notification with inline action buttons."""
        ...


# ============================================================
# ProcessCallWebhook
# ============================================================


class ProcessCallWebhook:
    """
    Оркестратор полного flow обработки звонка (ТЗ раздел 4.2):

    1. Получить CallRecord по call_id
    2. Скачать + транскрибировать аудио
    3. IdentifyAgentByVoice → биометрия
    4. Обновить статус CallRecord → PREVIEW + DraftSession
    5. SendCallNotification в Telegram
    6. При ошибке → mark_error() + логирование
    """

    def __init__(
        self,
        call_repo: CallRecordRepository,
        identify_agent: IdentifyAgentByVoice,
        notification_port: TelegramNotificationPort,
        dispatcher_chat_id: int,
    ) -> None:
        self._call_repo = call_repo
        self._identify_agent = identify_agent
        self._notification_port = notification_port
        self._dispatcher_chat_id = dispatcher_chat_id

    async def execute(self, call_id: str) -> CallRecord | None:
        """Process the call end-to-end. Returns updated CallRecord or None if not found."""
        call_record = await self._call_repo.get_by_id(call_id)
        if call_record is None:
            logger.warning("ProcessCallWebhook: call_id %s not found", call_id)
            return None

        try:
            # Step 2: voice biometry
            await self._identify_agent.execute(call_record, audio_bytes=b"")

            # Step 3: transition to PREVIEW
            session_id = uuid.uuid4()
            call_record.mark_preview(session_id)
            await self._call_repo.save(call_record)

            # Step 4: Telegram notification
            await self._notification_port.send_call_notification(
                self._dispatcher_chat_id, call_record
            )

        except Exception:
            logger.exception("ProcessCallWebhook failed for call_id %s", call_id)
            call_record.mark_error()
            await self._call_repo.save(call_record)
            return call_record

        return call_record
