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
        client_phone: str | None = None,
        agent_phone: str | None = None,
        direction: str | None = None,
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

        # Resolve Operator by agent_phone — the semantic "our operator"
        # number. For INCOMING this is callee; for OUTGOING it is caller
        # (because operator is the one who DIALS on a callback). Looked up
        # against Operator.workPhone in Twenty. We don't auto-create here
        # on miss — leave operatorRel null so an admin attaches in UI.
        operator_id: str | None = None
        op_phone = agent_phone or callee_phone  # backward-compat
        if op_phone:
            try:
                op = await self._port.find_operator_by_phone(op_phone)
                if op and op.get("id"):
                    operator_id = str(op["id"])
            except Exception:
                logger.exception(
                    "Failed resolving operator for call %s phone=%s",
                    record.call_id, op_phone,
                )

        # Snapshot Task.povtornoeObrashchenie → CallRecord.callKind so the
        # call card carries its own первое/повторное flag (denormalized for
        # filtering in Twenty UI and reports).
        call_kind: str | None = None
        if task_id:
            try:
                t = await self._port.get_task(task_id)
                if t is not None:
                    call_kind = "REPEAT" if t.get("povtornoeObrashchenie") else "FIRST"
            except Exception:
                logger.exception(
                    "Failed reading Task.povtornoeObrashchenie task=%s", task_id,
                )

        transcript = record.get_best_transcription()
        # direction comes from ATS2 callType via the poller; default to
        # INCOMING for backward-compat (the historical Telegram path
        # never passes direction).
        direction = direction or "INCOMING"
        call_status = _STATUS_MAP.get(record.status, "ERROR")

        twenty_id: str | None = None
        was_created = False
        if existing is None:
            try:
                created = await self._port.create_call_record(
                    record.call_id,
                    caller_phone=record.caller_phone,
                    callee_phone=callee_phone,
                    client_phone=client_phone or record.caller_phone,
                    agent_phone=agent_phone,
                    direction=direction,
                    duration=record.duration,
                    call_status=call_status,
                    occurred_at=record.created_at,
                    transcript=transcript,
                    location_rel_id=location_id,
                    task_rel_id=task_id,
                    operator_rel_id=operator_id,
                    call_kind=call_kind,
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
            existing_client = existing.get("clientPhone") or {}
            client_already_set = bool(
                isinstance(existing_client, dict)
                and existing_client.get("primaryPhoneNumber")
            )
            existing_agent = existing.get("agentPhone") or {}
            agent_already_set = bool(
                isinstance(existing_agent, dict)
                and existing_agent.get("primaryPhoneNumber")
            )
            wants_update = bool(
                task_id
                or (transcript and not existing.get("transcript"))
                or (callee_phone and not callee_already_set)
                or (client_phone and not client_already_set)
                or (agent_phone and not agent_already_set)
                or (direction and existing.get("direction") != direction)
                or (operator_id and not existing.get("operatorRelId"))
                or (call_kind and not existing.get("callKind"))
            )
            if twenty_id and wants_update:
                try:
                    await self._port.update_call_record(
                        twenty_id,
                        task_rel_id=task_id,
                        location_rel_id=location_id if not existing.get("locationRelId") else None,
                        transcript=transcript if not existing.get("transcript") else None,
                        callee_phone=callee_phone if not callee_already_set else None,
                        client_phone=client_phone if not client_already_set else None,
                        agent_phone=agent_phone if not agent_already_set else None,
                        direction=direction if existing.get("direction") != direction else None,
                        operator_rel_id=operator_id if not existing.get("operatorRelId") else None,
                        call_kind=call_kind if not existing.get("callKind") else None,
                    )
                except Exception:
                    logger.exception("Failed updating Twenty CallRecord %s", twenty_id)

        # Per-call script-check. Each CR gets its own scriptViolations /
        # scriptMissing — the task-level total is derived afterwards.
        if (
            twenty_id
            and transcript
            and record.status in {CallStatus.CREATED, CallStatus.PREVIEW, CallStatus.PROCESSING}
            and self._script_ai is not None
        ):
            try:
                await self._run_script_check(twenty_id, transcript)
            except Exception:
                logger.exception(
                    "check_script hook failed for cr %s task %s",
                    twenty_id, task_id,
                )

        # Refresh Task aggregates (scriptViolationsTotal + callRecordCount)
        # after every sync — cheap snapshot for flat reporting.
        if task_id:
            try:
                await self._port.recompute_task_aggregates(task_id)
            except Exception:
                logger.exception(
                    "recompute_task_aggregates failed task=%s", task_id,
                )

        return SyncResult(
            twenty_id=twenty_id,
            created=was_created,
            linked_task=bool(task_id),
        )

    async def _run_script_check(
        self, call_record_id: str, transcript: str,
    ) -> None:
        """Write per-call script-check result on the CallRecord."""
        if self._script_ai is None:
            return
        result = await self._script_ai.check_script(transcript)
        violations = int(result.get("violations_count") or 0)
        missing_raw = result.get("missing") or []
        missing_ids = [str(m) for m in missing_raw if isinstance(m, str)]
        missing_phrases = [SCRIPT_PHRASES_RU.get(m, m) for m in missing_ids]
        await self._port.update_call_record_script_check(
            call_record_id, violations, missing_phrases,
        )
