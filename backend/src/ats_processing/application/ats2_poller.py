"""ATS2 Poller Service — фоновый сервис периодического опроса ATS2 API."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis as AsyncRedis

from ai_classification.domain.repository import AIClassificationPort
from telegram_ingestion.application.ports import STTPort
from twenty_integration.application.classify_call_intent import (
    KIND_NO_ACTION,
    ClassifyCallIntent,
    IntentResult,
)
from twenty_integration.application.detect_repeat import DetectRepeat, RepeatResult
from twenty_integration.application.resolve_location import ResolveLocation
from twenty_integration.domain.ports import TwentyCRMPort

from ..domain.models import CallRecord, SourceType
from ..domain.repository import CallRecordRepository
from .ats2_transcription_mapper import ATS2TranscriptionMapper, ATS2Word
from .ports import ATS2CallSourcePort
from .sync_call_to_twenty import SyncCallToTwentyUseCase

_REDIS_LAST_POLL_KEY = "ats2_poller:last_poll_timestamp"

# ATS publishes a call only AFTER the conversation ends (plus some delay).
# Without an overlap, calls that started in cycle N-1 but became visible
# only in cycle N fall out of every window (date filter uses call start).
# 15 min covers the longest calls we observe; duplicates are filtered by
# `existing is not None: continue` in poll_once.
_POLL_OVERLAP_MIN = 15

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
        sync_call_uc: SyncCallToTwentyUseCase | None = None,
        location_resolver: ResolveLocation | None = None,
        detect_repeat: DetectRepeat | None = None,
        classify_intent: ClassifyCallIntent | None = None,
    ) -> None:
        self._ats2_client = ats2_client
        self._call_repo = call_repo
        self._mapper = transcription_mapper
        self._ai_port = ai_port
        self._twenty_port = twenty_port
        self._stt_port = stt_port
        self._redis = redis
        self._poll_interval_sec = poll_interval_sec
        self._sync_call_uc = sync_call_uc
        self._location_resolver = location_resolver
        self._detect_repeat = detect_repeat
        self._classify_intent = classify_intent
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
        # Widen `date_from` by _POLL_OVERLAP_MIN so late-published calls
        # (ATS publishes only after conversation ends; for long calls
        # the publish moment lands 1-2 cycles after the start time,
        # and the start-time filter drops them permanently).
        cursor = self._last_poll_timestamp  # type: ignore[assignment]
        date_from = cursor - timedelta(minutes=_POLL_OVERLAP_MIN) if cursor else now
        try:
            raw_calls = await self._ats2_client.get_call_records(
                date_from=date_from,
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
        """Обработать новый звонок: сохранить → транскрипция → AI → задача.

        Branches on callType:
          - SINGLE_CHANNEL / MULTI_CHANNEL → INCOMING: client=caller, agent=callee.
            Whisper + AI classify → create Task → mirror CallRecord.
          - OUTGOING / CRM_OUTGOING → OUTGOING (callback): client=callee,
            agent=caller. Whisper + mirror CallRecord BUT no Task. Try to
            attach to a recent INCOMING Task of this client; orphan if not.
        Other types are saved + logged but otherwise skipped.
        """
        filename = str(raw_call.get("recordFileName", ""))
        raw_caller = str(raw_call.get("callerNumber", "")) or None
        raw_callee = str(raw_call.get("calleeNumber", "")) or None
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

        # Map ATS callType → our 2-state direction + role assignment.
        # OUTGOING/CRM_OUTGOING are callbacks: caller is OUR operator,
        # callee is the customer. INCOMING (single/multi-channel): caller
        # is the customer, callee is the line they reached.
        is_outgoing = call_type in ("OUTGOING", "CRM_OUTGOING")
        direction_value = "OUTGOING" if is_outgoing else "INCOMING"
        if is_outgoing:
            client_phone = raw_callee
            agent_phone = raw_caller
        else:
            client_phone = raw_caller
            agent_phone = raw_callee
        # caller_phone here keeps the SEMANTIC client identity — used for
        # Location resolve, Task.callerPhone, and (later) recent-task lookup.
        caller_phone = client_phone

        # Получить транскрипцию: Whisper (Groq) → fallback ATS2 STT.
        # Comparing both on the same call (см. /tmp/compare_stt.py) showed
        # Whisper resolves time- and address-words noticeably better; ATS2
        # STT mangles key fragments ("Мы до пяти не успели" → "надо пить
        # и не успели"). We keep ATS2 STT as a safety net for когда
        # Groq падает или upload не доходит.
        transcription_text: str | None = None
        if filename:
            # Попытка 1: Whisper по аудио
            if self._stt_port is not None:
                try:
                    audio_bytes = await self._ats2_client.download_recording(filename)
                    transcription_text = await self._stt_port.transcribe(audio_bytes)
                    logger.info(
                        "ATS2 Poller: Whisper транскрипция для %s (%d bytes)",
                        call_id,
                        len(audio_bytes),
                    )
                except Exception:
                    logger.warning("ATS2 Poller: Whisper не смог для %s — пробую ATS2 STT", call_id)

            # Попытка 2 (fallback): ATS2 STT
            if not transcription_text:
                try:
                    raw_transcription = await self._ats2_client.get_transcription(filename)
                    raw_words: Any = raw_transcription.get("words", [])
                    if isinstance(raw_words, list) and raw_words:
                        words = [ATS2Word(**w) for w in raw_words]
                        transcription_text = self._mapper.map_to_dialogue(words)
                        logger.info(
                            "ATS2 Poller: fallback ATS2 STT для %s",
                            call_id,
                        )
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
            call_id,
            caller_phone,
            duration,
            bool(transcription_text),
        )

        # AI-анализ + создание задачи в Twenty (только INCOMING).
        #
        # Stage 1 — binary intent gate: создаём Task только если в звонке
        # реально есть запрос/проблема. NO_ACTION (мусор/недозвон/ack) —
        # CR всё равно попадает в Twenty (зеркалится ниже без taskRel),
        # но Task не плодится. На сомнении всегда создаём — потеря
        # настоящей заявки дороже пустышки.
        task_id: str | None = None
        intent: IntentResult | None = None
        if (not is_outgoing
                and transcription_text
                and self._ai_port
                and self._twenty_port):
            if self._classify_intent is not None:
                try:
                    intent = await self._classify_intent.execute(
                        transcript=transcription_text,
                        duration_sec=duration,
                    )
                    logger.info(
                        "ATS2 intent for %s: kind=%s conf=%.2f reason=%s",
                        call_id, intent.kind,
                        intent.confidence, intent.reason,
                    )
                except Exception:
                    logger.exception(
                        "ATS2 Poller: intent classify failed for %s — "
                        "defaulting to NEW_TASK", call_id,
                    )
                    intent = None

            if intent is not None and intent.kind == KIND_NO_ACTION:
                logger.info(
                    "ATS2 call %s — NO_ACTION (%s) — skipping Task creation",
                    call_id, intent.reason,
                )
            else:
                task_id = await self._create_task_from_call(
                    call_id=call_id,
                    transcription=transcription_text,
                    caller_phone=client_phone,
                    caller_name=caller_name,
                    callee_name=callee_name,
                    callee_phone=agent_phone,
                    duration=duration,
                    call_date=call_date,
                    call_type=call_type,
                    call_status=call_status,
                    destination=destination,
                )
                if task_id:
                    record.twenty_task_id = task_id
                    record.mark_created()
                else:
                    record.mark_error()
            await self._call_repo.save(record)

        # OUTGOING: попытаться прицепиться к недавней Task этого клиента
        # (создаёт ли клиент новый тикет — решает он сам, OUT-звонок не
        # должен порождать тикет).
        if (is_outgoing
                and self._twenty_port is not None
                and client_phone):
            try:
                since = datetime.now(UTC) - timedelta(days=30)
                parent = await self._twenty_port.find_recent_task_by_caller_phone(
                    client_phone, since,
                )
                if parent and parent.get("id"):
                    task_id = str(parent["id"])
                    record.twenty_task_id = task_id
                    record.mark_created()
                    await self._call_repo.save(record)
            except Exception:
                logger.exception(
                    "ATS2 Poller: failed attaching OUTGOING %s to existing Task",
                    call_id,
                )

        # Mirror the call into Twenty CallRecord (всегда — и INCOMING, и
        # OUTGOING). taskRel populated when we have a task to attach.
        if (transcription_text or is_outgoing) and self._sync_call_uc is not None:
            try:
                await self._sync_call_uc.execute(
                    record,
                    task_id=task_id,
                    callee_phone=agent_phone,  # legacy raw callee
                    client_phone=client_phone,
                    agent_phone=agent_phone,
                    direction=direction_value,
                )
            except Exception:
                logger.exception(
                    "ATS2 Poller: sync_call_uc failed for call %s task %s",
                    call_id, task_id,
                )

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
    ) -> str | None:
        """AI-анализ транскрипции → создание задачи в Twenty. Returns task id."""
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
                label = (
                    f"{callee_name} ({callee_phone})"
                    if callee_name and callee_phone
                    else (callee_name or callee_phone or "")
                )
                body_parts.append(f"- Принял: {label}")
            if duration:
                mins, secs = divmod(duration, 60)
                body_parts.append(f"- Длительность разговора: {mins}м {secs}с")
            body_parts.append(f"\n**Транскрипция:**\n{transcription}")

            # Подобрать kategoriya и vazhnost из актуальных списков Twenty.
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

            # Resolve the outlet (Location) for this incoming call —
            # cheap fold-match → AI extract → phone fallback. The
            # resolver also feeds caller_phone back into Location's
            # additionalPhones (learn-by-resolve) on a name/AI hit.
            location_rel_id: str | None = None
            if self._location_resolver is not None:
                try:
                    location_rel_id = await self._location_resolver.execute(
                        caller_phone=caller_phone,
                        dialogue_text=transcription,
                    )
                except Exception:
                    logger.exception(
                        "ATS2 Poller: location resolve failed for %s", call_id,
                    )

            # Detect repeat обращения: было ли недавно (3 дня) задание
            # на этой же точке с такой же сутью? Без этого все ATS-Task
            # создавались с povtornoeObrashchenie=false → M6 в отчётах
            # был занижен. Skipping — это та же ветка, что в Telegram-flow.
            repeat = RepeatResult()
            if self._detect_repeat is not None:
                try:
                    repeat = await self._detect_repeat.execute(
                        location_id=location_rel_id,
                        client_phone=caller_phone,
                        new_dialogue=transcription,
                    )
                except Exception:
                    logger.exception(
                        "ATS2 Poller: DetectRepeat failed for %s", call_id,
                    )

            # Resolve assignee from Operator: callee_phone is the agent
            # number, find_operator_by_phone returns the Operator entity,
            # and Operator.memberRel points to the WorkspaceMember who
            # answered. That same member becomes the Task assignee, so
            # closure metrics (M1–M5) credit the right person.
            assignee_id: str | None = None
            if callee_phone:
                try:
                    op = await self._twenty_port.find_operator_by_phone(callee_phone)
                    if op:
                        member_id = op.get("memberRelId")
                        if member_id:
                            assignee_id = str(member_id)
                except Exception:
                    logger.exception(
                        "ATS2 Poller: assignee resolve failed for %s", call_id,
                    )

            task = await self._twenty_port.create_task(
                title=f"📞 {classification.title}",
                body="\n".join(body_parts),
                due_at=call_datetime,
                assignee_id=assignee_id,
                kategoriya=kategoriya_value,
                vazhnost=vazhnost_value,
                location_rel_id=location_rel_id,
                caller_phone=caller_phone,
                povtornoe_obrashchenie=repeat.is_repeat,
                parent_task_id=repeat.parent_task_id,
                istochnik="ZVONOK",
                obrashchenie_kind=repeat.chain_position,
            )
            logger.info(
                "ATS2 call %s → Twenty task created: %s (loc=%s)",
                call_id, task.twenty_id, location_rel_id or "—",
            )
            return task.twenty_id
        except Exception:
            logger.exception("ATS2 Poller: ошибка создания задачи для %s", call_id)
            return None
