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
    ) -> SyncResult:
        # Prefer the locally persisted twenty_task_id (set by
        # CreateTwentyTaskFromSession) so the backfill also links calls
        # to tasks without the caller having to pass task_id.
        task_id = task_id or record.twenty_task_id
        existing = await self._port.find_call_record_by_ats_id(record.call_id)

        # Resolve Person/Location only when needed:
        #   - new CallRecord (existing is None) — always;
        #   - existing CallRecord — only if its relations are still empty
        #     (historical records from before the phone-based sync).
        # Re-running the backfill must NOT create duplicate Person/Location
        # rows for already-synced calls. That was the bug.
        need_resolve = (
            record.caller_phone
            and (existing is None
                 or not existing.get("personRelId")
                 or not existing.get("locationRelId"))
        )
        person_id: str | None = None
        location_id: str | None = None
        if need_resolve and record.caller_phone:
            try:
                person = await self._port.find_person_by_phone(record.caller_phone)
                if person is None:
                    person = await self._port.create_person_with_phone(record.caller_phone)
                person_id = str(person.get("id") or "") or None
                location = await self._port.find_location_by_phone(record.caller_phone)
                if location is None:
                    location = await self._port.create_location(record.caller_phone)
                location_id = str(location.get("id") or "") or None
            except Exception:
                logger.exception(
                    "Failed resolving person/location for call %s", record.call_id
                )

        transcript = record.get_best_transcription()
        direction = "INCOMING"  # ATS2 doesn't tell us direction in current poller
        call_status = _STATUS_MAP.get(record.status, "ERROR")

        if existing is None:
            try:
                created = await self._port.create_call_record(
                    record.call_id,
                    caller_phone=record.caller_phone,
                    direction=direction,
                    duration=record.duration,
                    call_status=call_status,
                    occurred_at=record.created_at,
                    transcript=transcript,
                    person_rel_id=person_id,
                    location_rel_id=location_id,
                    task_rel_id=task_id,
                )
                twenty_id = str(created.get("id") or "") or None
                return SyncResult(
                    twenty_id=twenty_id,
                    created=True,
                    linked_task=bool(task_id),
                )
            except Exception:
                logger.exception("Failed creating Twenty CallRecord for %s", record.call_id)
                return SyncResult(twenty_id=None, created=False, linked_task=False)

        twenty_id = str(existing.get("id") or "") or None
        if twenty_id and (task_id or (transcript and not existing.get("transcript"))):
            try:
                await self._port.update_call_record(
                    twenty_id,
                    task_rel_id=task_id,
                    person_rel_id=person_id if not existing.get("personRelId") else None,
                    location_rel_id=location_id if not existing.get("locationRelId") else None,
                    transcript=transcript if not existing.get("transcript") else None,
                )
            except Exception:
                logger.exception("Failed updating Twenty CallRecord %s", twenty_id)

        # Script check on the first answered call for this task (Stage 7).
        # We run it at most once per task: skipped if the task already has a
        # scriptViolations value on record.
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
            created=False,
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
        missing = [str(m) for m in missing_raw if isinstance(m, str)]
        await self._port.update_task_script_check(task_id, violations, missing)
