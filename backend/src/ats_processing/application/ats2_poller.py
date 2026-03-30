"""ATS2 Poller Service — фоновый сервис периодического опроса ATS2 API (DEV-128)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from ..domain.models import CallRecord, SourceType
from ..domain.repository import CallRecordRepository
from .ats2_transcription_mapper import ATS2TranscriptionMapper, ATS2Word
from .ports import ATS2CallSourcePort
from .use_cases import ProcessCallWebhook

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SEC = 60.0


class ATS2PollerService:
    """
    Фоновый сервис периодического опроса ATS2 API для обнаружения новых звонков.

    - Хранит last_poll_timestamp для инкрементального опроса
    - Дедупликация по call_id
    - Для каждого нового звонка: создаёт CallRecord, скачивает запись,
      получает транскрипцию, запускает ProcessCallWebhook
    - Graceful shutdown через stop()
    """

    def __init__(
        self,
        ats2_client: ATS2CallSourcePort,
        call_repo: CallRecordRepository,
        process_call_webhook: ProcessCallWebhook,
        transcription_mapper: ATS2TranscriptionMapper,
        poll_interval_sec: float = _DEFAULT_POLL_INTERVAL_SEC,
    ) -> None:
        self._ats2_client = ats2_client
        self._call_repo = call_repo
        self._process_call = process_call_webhook
        self._mapper = transcription_mapper
        self._poll_interval_sec = poll_interval_sec
        self._last_poll_timestamp: datetime = datetime.now(UTC)
        self._running: bool = False
        self._stop_event: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        """Запустить цикл опроса. Блокирует до вызова stop()."""
        self._running = True
        self._stop_event.clear()
        logger.info("ATS2 Poller started, interval=%ss", self._poll_interval_sec)

        while self._running:
            await self.poll_once()

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_sec,
                )
                break
            except TimeoutError:
                continue

        logger.info("ATS2 Poller stopped")

    def stop(self) -> None:
        """Graceful shutdown — завершить текущий цикл и выйти."""
        self._running = False
        self._stop_event.set()

    async def poll_once(self) -> None:
        """Выполнить один цикл опроса ATS2 API."""
        now = datetime.now(UTC)

        try:
            raw_calls = await self._ats2_client.get_call_records(
                date_from=self._last_poll_timestamp,
                date_to=now,
            )
        except Exception:
            logger.exception("ATS2 Poller: ошибка при запросе call records")
            return

        for raw_call in raw_calls:
            call_id = str(raw_call.get("id", ""))
            if not call_id:
                continue

            # Дедупликация
            existing = await self._call_repo.get_by_id(call_id)
            if existing is not None:
                logger.debug("ATS2 Poller: пропуск дубликата call_id=%s", call_id)
                continue

            await self._process_new_call(raw_call, call_id)

        # Обновляем timestamp только при успешном опросе
        self._last_poll_timestamp = now

    async def _process_new_call(
        self, raw_call: dict[str, object], call_id: str
    ) -> None:
        """Обработать один новый звонок: создать запись, скачать аудио, транскрипцию."""
        filename = str(raw_call.get("filename", ""))

        # Скачать запись
        try:
            await self._ats2_client.download_recording(filename)
        except Exception:
            logger.exception(
                "ATS2 Poller: ошибка загрузки записи call_id=%s", call_id
            )

        # Получить транскрипцию
        transcription_text: str | None = None
        try:
            raw_transcription = await self._ats2_client.get_transcription(filename)
            raw_words = raw_transcription.get("words", [])
            if isinstance(raw_words, list) and raw_words:
                words = [ATS2Word(**w) for w in raw_words]  # type: ignore[arg-type]
                transcription_text = self._mapper.map_to_dialogue(words)
        except Exception:
            logger.exception(
                "ATS2 Poller: ошибка транскрипции call_id=%s", call_id
            )

        # Создать CallRecord
        audio_url = f"ats2://recordings/{filename}" if filename else ""
        record = CallRecord(
            call_id=call_id,
            audio_url=audio_url,
            source=SourceType.CALL_ATS2_POLLING,
            transcription_t2=transcription_text,
            duration=int(raw_call["duration"]) if raw_call.get("duration") else None,
            caller_phone=str(raw_call.get("callerPhone", "")) or None,
            agent_ext=str(raw_call.get("agentExt", "")) or None,
        )

        await self._call_repo.save(record)

        # Запустить ProcessCallWebhook
        try:
            await self._process_call.execute(call_id)
        except Exception:
            logger.exception(
                "ATS2 Poller: ошибка ProcessCallWebhook call_id=%s", call_id
            )
