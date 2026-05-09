"""SyncCallToTwentyUseCase — project local ats_call_records into Twenty CallRecord.

Idempotent: upserts by atsCallId (the primary key we own). Called in two
places:

  1. Live path: after ATS2 poller finishes processing a call
     (answered, missed, or errored — all go to Twenty so admins see
     the full picture in the CRM UI).
  2. Batch path: backfill_call_records.py iterates historical
     ats_call_records and calls execute() for each.

The use case does NOT re-run AI or create a task — that's a separate
path. It just mirrors the operational record into Twenty, attaching
person/location relations if we can resolve them by phone.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from ai_classification.infrastructure.openrouter_adapter import SCRIPT_PHRASES_RU
from ats_processing.domain.models import CallRecord, CallStatus
from twenty_integration.domain.ports import TwentyCRMPort

logger = logging.getLogger(__name__)


class _CheckScriptPort(Protocol):
    async def check_script(self, dialogue_text: str) -> dict[str, Any]: ...


_STATUS_MAP: dict[CallStatus, str] = {
    CallStatus.CREATED: "ANSWERED",
    CallStatus.PREVIEW: "ANSWERED",
    CallStatus.PROCESSING: "ANSWERED",
    CallStatus.NEW: "MISSED",
    CallStatus.ERROR: "ERROR",
}


@dataclass
class SyncResult:
    twenty_id: str | None
    created: bool  # True if a new CallRecord was created, False if found existing
    linked_task: bool


class SyncCallToTwentyUseCase:
    """Mirror a local CallRecord into Twenty. Idempotent by atsCallId."""

    def __init__(
        self,
        twenty_port: TwentyCRMPort,
        script_ai: _CheckScriptPort | None = None,
    ) -> None:
        self._port = twenty_port
        self._script_ai = script_ai

    async def execute(
        self,
        record: CallRecord,
        *,
        task_id: str | None = None,
        callee_phone: str | None = None,
    ) -> SyncResult:
        # Prefer the locally persisted twenty_task_id (set by
        # CreateTwentyTaskFromSession) so the backfill also links calls
        # to tasks without the caller having to pass task_id.
        task_id = task_id or record.twenty_task_id
        existing = await self._port.find_call_record_by_ats_id(record.call_id)

        # Resolve Location only — Person для клиентов больше не создаём,
        # caller phone живёт прямо на CallRecord.callerPhone и Task.callerPhone.
        # Привязываем CallRecord к точке, только если по телефону однозначно
        # определилась одна. >1 кандидатов (выездной менеджер) или 0 →
        # CallRecord идёт без locationRelId, оператор вяжет в UI.
        need_resolve = (
            record.caller_phone
            and (existing is None or not existing.get("locationRelId"))
        )
        location_id: str | None = None
        if need_resolve and record.caller_phone:
            try:
                candidates = await self._port.find_locations_by_phone(record.caller_phone)
                if len(candidates) == 1:
                    location_id = str(candidates[0].get("id") or "") or None
                elif len(candidates) > 1:
                    logger.warning(
                        "ambiguous location for call %s phone=%s: %d candidates",
                        record.call_id, record.caller_phone, len(candidates),
                    )
            except Exception:
                logger.exception(
                    "Failed resolving location for call %s", record.call_id
                )

        transcript = record.get_best_transcription()
        direction = "INCOMING"  # ATS2 doesn't tell us direction in current poller
        call_status = _STATUS_MAP.get(record.status, "ERROR")

        twenty_id: str | None = None
        was_created = False
        if existing is None:
            try:
                created = await self._port.create_call_record(
                    record.call_id,
                    caller_phone=record.caller_phone,
                    callee_phone=callee_phone,
                    direction=direction,
                    duration=record.duration,
                    call_status=call_status,
                    occurred_at=record.created_at,
                    transcript=transcript,
                    location_rel_id=location_id,
                    task_rel_id=task_id,
                )
                twenty_id = str(created.get("id") or "") or None
                was_created = True
            except Exception:
                logger.exception("Failed creating Twenty CallRecord for %s", record.call_id)
                return SyncResult(twenty_id=None, created=False, linked_task=False)
        else:
            twenty_id = str(existing.get("id") or "") or None
            existing_callee = existing.get("calleePhone") or {}
            callee_already_set = bool(
                isinstance(existing_callee, dict)
                and existing_callee.get("primaryPhoneNumber")
            )
            wants_update = bool(
                task_id
                or (transcript and not existing.get("transcript"))
                or (callee_phone and not callee_already_set)
            )
            if twenty_id and wants_update:
                try:
                    await self._port.update_call_record(
                        twenty_id,
                        task_rel_id=task_id,
                        location_rel_id=location_id if not existing.get("locationRelId") else None,
                        transcript=transcript if not existing.get("transcript") else None,
                        callee_phone=callee_phone if not callee_already_set else None,
                    )
                except Exception:
                    logger.exception("Failed updating Twenty CallRecord %s", twenty_id)

        # Script check on the first answered call for this task (Stage 7).
        # Runs on BOTH the create and update paths: a freshly-synced call
        # (was_created=True) still needs its transcript evaluated, otherwise
        # the first call on every new task silently skips scriptViolations.
        if (
            task_id
            and transcript
            and record.status in {CallStatus.CREATED, CallStatus.PREVIEW, CallStatus.PROCESSING}
            and self._script_ai is not None
        ):
            try:
                await self._run_script_check(task_id, transcript)
            except Exception:
                logger.exception("check_script hook failed for task %s", task_id)

        return SyncResult(
            twenty_id=twenty_id,
            created=was_created,
            linked_task=bool(task_id),
        )

    async def _run_script_check(self, task_id: str, transcript: str) -> None:
        if self._script_ai is None:
            return
        existing = await self._port.get_task(task_id)
        if existing is None:
            return
        if existing.get("scriptViolations") is not None:
            return  # already checked on a prior call
        result = await self._script_ai.check_script(transcript)
        violations = int(result.get("violations_count") or 0)
        missing_raw = result.get("missing") or []
        missing_ids = [str(m) for m in missing_raw if isinstance(m, str)]
        missing_phrases = [SCRIPT_PHRASES_RU.get(m, m) for m in missing_ids]
        await self._port.update_task_script_check(task_id, violations, missing_phrases)
