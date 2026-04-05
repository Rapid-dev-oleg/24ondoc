"""ATS2 Poller Service — фоновый сервис периодического опроса ATS2 API."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_classification.domain.repository import AIClassificationPort
from redis.asyncio import Redis as AsyncRedis
from telegram_ingestion.application.ports import STTPort
from twenty_integration.domain.ports import TwentyCRMPort

from ..domain.models import CallRecord, SourceType
from ..domain.repository import CallRecordRepository
from .ats2_transcription_mapper import ATS2TranscriptionMapper, ATS2Word
from .ports import ATS2CallSourcePort

_REDIS_LAST_POLL_KEY = "ats2_poller:last_poll_timestamp"

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SEC = 60.0


class ATS2PollerService:
    """
    Фоновый сервис: опрашивает ATS2 API → получает транскрипцию →
    AI-анализ → создаёт задачу в Twenty CRM.
    """

    def __init__(
        self,
        ats2_client: ATS2CallSourcePort,
        call_repo: CallRecordRepository,
        transcription_mapper: ATS2TranscriptionMapper,
        ai_port: AIClassificationPort | None = None,
        twenty_port: TwentyCRMPort | None = None,
        stt_port: STTPort | None = None,
        redis: AsyncRedis | None = None,
        poll_interval_sec: float = _DEFAULT_POLL_INTERVAL_SEC,
    ) -> None:
        self._ats2_client = ats2_client
        self._call_repo = call_repo
        self._mapper = transcription_mapper
        self._ai_port = ai_port
        self._twenty_port = twenty_port
        self._stt_port = stt_port
        self._redis = redis
        self._poll_interval_sec = poll_interval_sec
        self._last_poll_timestamp: datetime | None = None
        self._running: bool = False
        self._stop_event: asyncio.Event = asyncio.Event()

    async def _load_last_poll_timestamp(self) -> datetime:
        """Load from Redis or fallback to 1 hour ago."""
        if self._redis is not None:
            raw = await self._redis.get(_REDIS_LAST_POLL_KEY)
            if raw is not None:
                try:
                    ts = datetime.fromisoformat(raw.decode())
                    logger.info("ATS2 Poller: restored timestamp from Redis: %s", ts)
                    return ts
                except (ValueError, AttributeError):
                    pass
        return datetime.now(UTC) - timedelta(hours=1)

    async def _save_last_poll_timestamp(self, ts: datetime) -> None:
        """Persist to Redis."""
        if self._redis is not None:
            await self._redis.set(_REDIS_LAST_POLL_KEY, ts.isoformat())

    async def start(self) -> None:
        """Запустить цикл опроса."""
        self._last_poll_timestamp = await self._load_last_poll_timestamp()
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
        self._running = False
        self._stop_event.set()

    async def poll_once(self) -> None:
        """Один цикл опроса."""
        now = datetime.now(UTC)
        try:
            raw_calls = await self._ats2_client.get_call_records(
                date_from=self._last_poll_timestamp,
                date_to=now,
            )
        except Exception:
            logger.exception("ATS2 Poller: ошибка при запросе call records")
            return

        new_count = 0
        for raw_call in raw_calls:
            call_id = str(raw_call.get("uuid", ""))
            if not call_id:
                continue

            existing = await self._call_repo.get_by_id(call_id)
            if existing is not None:
                continue

            await self._process_new_call(raw_call, call_id)
            new_count += 1

        if new_count > 0:
            logger.info("ATS2 Poller: обработано %d новых звонков", new_count)

        self._last_poll_timestamp = now
        await self._save_last_poll_timestamp(now)

    async def _process_new_call(self, raw_call: dict[str, object], call_id: str) -> None:
        """Обработать новый звонок: сохранить → транскрипция → AI → задача."""
        filename = str(raw_call.get("recordFileName", ""))
        caller_phone = str(raw_call.get("callerNumber", "")) or None
        callee_phone = str(raw_call.get("calleeNumber", "")) or None
        caller_name = str(raw_call.get("callerName", "")) or None
        callee_name = str(raw_call.get("calleeName", "")) or None
        call_date = str(raw_call.get("date", "")) or None
        call_type = str(raw_call.get("callType", "")) or None
        call_status = str(raw_call.get("callStatus", "")) or None
        destination = str(raw_call.get("destinationNumber", "")) or None
        duration = None
        if raw_call.get("conversationDuration"):
            try:
                duration = int(str(raw_call["conversationDuration"]))
            except (ValueError, TypeError):
                pass

        # Получить транскрипцию: ATS2 STT → fallback Whisper
        transcription_text: str | None = None
        if filename:
            # Попытка 1: ATS2 STT
            try:
                raw_transcription = await self._ats2_client.get_transcription(filename)
                raw_words: Any = raw_transcription.get("words", [])
                if isinstance(raw_words, list) and raw_words:
                    words = [ATS2Word(**w) for w in raw_words]
                    transcription_text = self._mapper.map_to_dialogue(words)
            except Exception:
                pass

            # Попытка 2: скачать аудио + Whisper
            if not transcription_text and self._stt_port is not None:
                try:
                    audio_bytes = await self._ats2_client.download_recording(filename)
                    transcription_text = await self._stt_port.transcribe(audio_bytes)
                    logger.info("ATS2 Poller: Whisper транскрипция для %s (%d bytes)", call_id, len(audio_bytes))
                except Exception:
                    logger.warning("ATS2 Poller: транскрипция недоступна для %s", call_id)

        # Сохранить CallRecord
        audio_url = f"ats2://recordings/{filename}" if filename else ""
        record = CallRecord(
            call_id=call_id,
            audio_url=audio_url,
            source=SourceType.CALL_ATS2_POLLING,
            transcription_t2=transcription_text,
            duration=duration,
            caller_phone=caller_phone,
        )
        await self._call_repo.save(record)
        logger.info(
            "ATS2 call saved: %s, phone=%s, duration=%s, has_transcription=%s",
            call_id, caller_phone, duration, bool(transcription_text),
        )

        # AI-анализ + создание задачи в Twenty
        if transcription_text and self._ai_port and self._twenty_port:
            success = await self._create_task_from_call(
                call_id=call_id,
                transcription=transcription_text,
                caller_phone=caller_phone,
                caller_name=caller_name,
                callee_name=callee_name,
                callee_phone=callee_phone,
                duration=duration,
                call_date=call_date,
                call_type=call_type,
                call_status=call_status,
                destination=destination,
            )
            if success:
                record.mark_created()
            else:
                record.mark_error()
            await self._call_repo.save(record)

    async def _create_task_from_call(
        self,
        call_id: str,
        transcription: str,
        caller_phone: str | None,
        caller_name: str | None,
        callee_name: str | None,
        callee_phone: str | None = None,
        duration: int | None = None,
        call_date: str | None = None,
        call_type: str | None = None,
        call_status: str | None = None,
        destination: str | None = None,
    ) -> bool:
        """AI-анализ транскрипции → создание задачи в Twenty."""
        assert self._ai_port is not None
        assert self._twenty_port is not None

        # Парсим дату звонка
        call_datetime: datetime | None = None
        call_date_display = ""
        if call_date:
            try:
                call_datetime = datetime.fromisoformat(call_date.replace("Z", "+00:00"))
                # Конвертируем в UTC для Twenty API
                call_datetime = call_datetime.astimezone(UTC)
                call_date_display = call_datetime.strftime("%d.%m.%Y %H:%M")
            except (ValueError, AttributeError):
                call_date_display = call_date

        # Собрать контекст для AI
        context_parts = [f"Транскрипция звонка (длительность: {duration}с):"]
        if call_date_display:
            context_parts.append(f"Дата звонка: {call_date_display}")
        if caller_phone:
            context_parts.append(f"Телефон звонящего: {caller_phone}")
        if caller_name:
            context_parts.append(f"Имя звонящего: {caller_name}")
        context_parts.append("")
        context_parts.append(transcription)
        full_text = "\n".join(context_parts)

        try:
            classification = await self._ai_port.classify(full_text)

            # Маппинг типов и статусов
            type_labels = {
                "SINGLE_CHANNEL": "Входящий",
                "MULTI_CHANNEL": "Входящий (многоканальный)",
                "OUTGOING": "Исходящий",
                "INTERNAL": "Внутренний",
                "CRM_OUTGOING": "Исходящий (CRM)",
                "CALLBACK": "Обратный звонок",
            }
            status_labels = {
                "ANSWERED_COMMON": "Отвечен",
                "ANSWERED_BY_ORIGINAL_CLIENT": "Отвечен",
                "NOT_ANSWERED_COMMON": "Пропущен",
                "CANCELLED_BY_CALLER": "Отменён звонящим",
                "DENIED_DUE_TO_BLACK_LISTED": "Чёрный список",
                "DESTINATION_BUSY": "Занято",
            }

            body_parts = [classification.description]
            body_parts.append("\n\n---\n**Данные звонка:**")
            if call_date_display:
                body_parts.append(f"- Дата: {call_date_display}")
            if call_type:
                body_parts.append(f"- Тип: {type_labels.get(call_type, call_type)}")
            if call_status:
                body_parts.append(f"- Статус: {status_labels.get(call_status, call_status)}")
            if caller_phone:
                label = f"{caller_name} ({caller_phone})" if caller_name else caller_phone
                body_parts.append(f"- Звонящий: {label}")
            if callee_phone or callee_name:
                label = f"{callee_name} ({callee_phone})" if callee_name and callee_phone else (callee_name or callee_phone)
                body_parts.append(f"- Принял: {label}")
            if duration:
                mins, secs = divmod(duration, 60)
                body_parts.append(f"- Длительность разговора: {mins}м {secs}с")
            body_parts.append(f"\n**Транскрипция:**\n{transcription}")

            # Подобрать kategoriya и vazhnost из актуальных списков Twenty
            kategoriya_value: str | None = None
            vazhnost_value: str | None = None
            try:
                options = await self._twenty_port.fetch_task_field_options()
                selection = await self._ai_port.select_task_fields(
                    full_text,
                    options.get("kategoriya", []),
                    options.get("vazhnost", []),
                )
                kategoriya_value = selection.kategoriya
                vazhnost_value = selection.vazhnost
            except Exception:
                logger.warning("ATS2 Poller: failed to select task fields for %s", call_id)

            task = await self._twenty_port.create_task(
                title=f"📞 {classification.title}",
                body="\n".join(body_parts),
                due_at=call_datetime,
                assignee_id=None,
                kategoriya=kategoriya_value,
                vazhnost=vazhnost_value,
            )
            logger.info(
                "ATS2 call %s → Twenty task created: %s", call_id, task.twenty_id
            )
            return True
        except Exception:
            logger.exception("ATS2 Poller: ошибка создания задачи для %s", call_id)
            return False
