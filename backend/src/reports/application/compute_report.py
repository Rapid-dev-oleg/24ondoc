"""Pure function: TimelineData + window → ReportDTO.

All formulas from the agreed per-operator report spec live here. No I/O,
no database — so it's fully deterministic and unit-testable.

Terminology (matches Twenty's real data, see earlier inspection):
- task.updated events carry a structured diff in `properties.diff`:
    {field: {before, after}}.
- `targetTaskId` on the event is the only reliable link back to the task
  (linkedRecordId is NULL on system-emitted events).
- `workspaceMemberId` on the event is the actor; may also be None for
  system/background changes.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from typing import Any

from ..domain.models import EmployeeRow, ReportDTO, ReportScope
from ..infrastructure.twenty_timeline_reader import TimelineData

TERMINAL_STATUSES = {"VYPOLNENO", "DONE"}
HIGH_PRIORITY = {"VYSOKAYA", "KRITICHNO"}


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_ts(e: dict[str, Any]) -> str | None:
    return e.get("happensAt") or e.get("createdAt")


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def compute_report(
    data: TimelineData,
    *,
    from_ts: datetime,
    to_ts: datetime,
    scope: ReportScope = ReportScope.OVERALL,
    user_id: str | None = None,
) -> ReportDTO:
    """Build the per-operator report + totals footer for [from_ts, to_ts]."""
    tasks_by_id = {t["id"]: t for t in data.tasks}

    # Real Twenty INSERT timestamp per task, from task.created events.
    # We prefer this over task.createdAt column because our backend
    # overwrites the column with the ATS call time during task creation,
    # so the column drifts from the actual CRM-appearance moment.
    task_created_ts: dict[str, datetime] = {}
    for e in data.created_events:
        tid = e.get("targetTaskId")
        ts = _parse_iso(_event_ts(e))
        if tid and ts and tid not in task_created_ts:
            task_created_ts[tid] = ts

    # Index events by task: sorted ascending by happensAt.
    events_per_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in data.updated_events:
        tid = e.get("targetTaskId")
        if tid:
            events_per_task[tid].append(e)
    for lst in events_per_task.values():
        lst.sort(key=lambda x: _event_ts(x) or "")

    # First terminal transition per task, if it lands in the window.
    completion_in_window: dict[str, dict[str, Any]] = {}  # tid -> event
    # First assignment to a given wm for response-time metric.
    first_assignment_event: dict[str, dict[str, Any]] = {}  # tid -> event
    for tid, evs in events_per_task.items():
        for e in evs:
            diff = (e.get("properties") or {}).get("diff") or {}
            st = diff.get("status")
            asg = diff.get("assigneeId")
            ts = _parse_iso(_event_ts(e))
            if st and st.get("after") in TERMINAL_STATUSES and \
                    st.get("before") not in TERMINAL_STATUSES:
                # Take the LAST in-window terminal transition, not the first:
                # tasks that were closed → reopened → closed again must count
                # from the final closure, otherwise duration reflects a stale
                # first attempt. Events are sorted ascending so simple overwrite
                # wins. M8 uses first_assignment_event, not this map, so it's
                # unaffected.
                if ts and from_ts <= ts <= to_ts:
                    completion_in_window[tid] = e
            if asg and asg.get("after") and not asg.get("before"):
                if tid not in first_assignment_event:
                    first_assignment_event[tid] = e

    # For each completed-in-window task, find `received_at(tid)`.
    #
    # Normal path: the happensAt of the LAST task.updated event before
    # completion where diff.assigneeId.after == owner.
    #
    # Atomic-save path: if the completion event ITSELF also carries the
    # assignment-to-owner diff (operator clicked "назначить себе" and
    # "выполнено" in one save — Twenty collapses to a single
    # timelineActivity with both diffs at identical happensAt), the
    # strict start/finish would be zero. Fall back to `task.created`
    # event timestamp so duration reflects the task's lifetime in CRM.
    #
    # No fallback to task.createdAt COLUMN — we backfill that to the
    # ATS call time, so it drifts from reality by hours/days.
    def _received_at(
        tid: str, owner: str, completion_ts: datetime, comp_event: dict[str, Any],
    ) -> datetime | None:
        comp_diff = (comp_event.get("properties") or {}).get("diff") or {}
        comp_asg = (comp_diff.get("assigneeId") or {})
        if comp_asg.get("after") == owner:
            return task_created_ts.get(tid)

        last: datetime | None = None
        for e in events_per_task.get(tid, []):
            diff = (e.get("properties") or {}).get("diff") or {}
            asg = diff.get("assigneeId") or {}
            if asg.get("after") != owner:
                continue
            ts = _parse_iso(_event_ts(e))
            if ts is None or ts > completion_ts:
                continue
            last = ts  # events are ascending → last wins
        return last

    # Per-owner accumulation.
    # Each bucket collects the raw numbers; we convert to EmployeeRow at the end.
    class _Acc:
        def __init__(self) -> None:
            self.completed_durations: list[float] = []
            self.complex_durations: list[float] = []
            self.repeats = 0
            self.script_violations = 0
            self.response_times: list[float] = []

    acc: dict[str | None, _Acc] = defaultdict(_Acc)

    # --- walk completed tasks ---
    for tid, comp_event in completion_in_window.items():
        if tid not in tasks_by_id:
            continue  # task was deleted — ignore its leftover events
        t = tasks_by_id[tid]
        if t.get("status") not in TERMINAL_STATUSES:
            continue  # closed in window but reopened afterwards — not "done"
        owner = t.get("assigneeId") or None  # may be None if unassigned when closed
        completion_ts = _parse_iso(_event_ts(comp_event))
        if completion_ts is None:
            continue
        if owner is None:
            continue  # unassigned completed task — skip from per-operator metrics
        received_ts = _received_at(tid, owner, completion_ts, comp_event)
        if received_ts is None:
            continue  # no assignment event AND no task.created anchor — skip
        duration: float | None = (completion_ts - received_ts).total_seconds()
        if duration < 0:
            duration = None

        bucket = acc[owner]
        if duration is not None:
            bucket.completed_durations.append(duration)
            if t.get("vazhnost") in HIGH_PRIORITY:
                bucket.complex_durations.append(duration)

    # --- walk tasks created in window (for repeats + script violations) ---
    # M6/M7 are about the intake side of the period (what came in + quality
    # of the first call), NOT the closure side, so we iterate created-in-
    # window independent of completion status.
    for t in data.tasks:
        created_ts = _parse_iso(t.get("createdAt"))
        if created_ts is None or not (from_ts <= created_ts <= to_ts):
            continue
        owner = t.get("assigneeId") or None
        bucket = acc[owner]
        if t.get("povtornoeObrashchenie"):
            bucket.repeats += 1
        bucket.script_violations += int(t.get("scriptViolations") or 0)

    # --- walk first-assignment events in window for response time ---
    # Uses task.created timeline ts (real Twenty INSERT moment), not the
    # backfilled task.createdAt column. Tasks with no task.created event
    # in timeline are skipped entirely — we can't compute a meaningful
    # response time without a trustworthy "task appeared" anchor. Tasks
    # that no longer exist (deleted in UI) are also skipped so the metric
    # doesn't linger on ghost rows after cleanup.
    for tid, e in first_assignment_event.items():
        if tid not in tasks_by_id:
            continue
        ts = _parse_iso(_event_ts(e))
        if ts is None or not (from_ts <= ts <= to_ts):
            continue
        created_ts = task_created_ts.get(tid)
        if created_ts is None:
            continue
        diff = (e.get("properties") or {}).get("diff") or {}
        assignee_after = (diff.get("assigneeId") or {}).get("after")
        if not assignee_after:
            continue
        delta = (ts - created_ts).total_seconds()
        if delta < 0:
            continue
        acc[assignee_after].response_times.append(delta)

    # --- pending snapshot (per current assignee) ---
    pending_by_owner: dict[str | None, int] = defaultdict(int)
    for t in data.tasks:
        if t.get("status") in TERMINAL_STATUSES:
            continue
        pending_by_owner[t.get("assigneeId") or None] += 1
    # Ensure pending-only owners surface as rows too:
    for owner in pending_by_owner:
        acc.setdefault(owner, _Acc())

    # --- build rows ---
    rows: list[EmployeeRow] = []
    for owner, a in acc.items():
        completed_n = len(a.completed_durations)
        total_dur = int(sum(a.completed_durations))
        avg_dur = _avg(a.completed_durations)
        complex_n = len(a.complex_durations)
        avg_cx = _avg(a.complex_durations)
        avg_resp = _avg(a.response_times)
        rows.append(EmployeeRow(
            user_id=owner,
            display_name=data.members_by_id.get(owner or "", "— не назначено" if owner is None else owner[:8]),
            completed=completed_n,
            total_duration_seconds=total_dur,
            avg_duration_seconds=avg_dur,
            complex_count=complex_n,
            avg_complex_duration_seconds=avg_cx,
            repeats_count=a.repeats,
            script_violations=a.script_violations,
            pending_count=pending_by_owner.get(owner, 0),
            avg_response_time_seconds=avg_resp,
        ))

    # Filter rows by scope
    if scope in (ReportScope.SELF, ReportScope.EMPLOYEE) and user_id:
        rows = [r for r in rows if r.user_id == user_id]
    # Overall view: sort by completed desc, but pin the "— не назначено"
    # bucket to the top so its intake (pending + script violations)
    # is immediately visible.
    rows.sort(key=lambda r: (r.user_id is not None, -r.completed))

    # --- totals row (weighted, not mean-of-means) ---
    all_durs: list[float] = []
    all_cx: list[float] = []
    all_resp: list[float] = []
    repeats_total = 0
    script_total = 0
    for a in acc.values():
        all_durs.extend(a.completed_durations)
        all_cx.extend(a.complex_durations)
        all_resp.extend(a.response_times)
        repeats_total += a.repeats
        script_total += a.script_violations
    total_pending = sum(pending_by_owner.values())
    totals = EmployeeRow(
        user_id=None,
        display_name="Итого",
        completed=len(all_durs),
        total_duration_seconds=int(sum(all_durs)),
        avg_duration_seconds=_avg(all_durs),
        complex_count=len(all_cx),
        avg_complex_duration_seconds=_avg(all_cx),
        repeats_count=repeats_total,
        script_violations=script_total,
        pending_count=total_pending,
        avg_response_time_seconds=_avg(all_resp),
    )

    # When scoped to one employee, count only their created-in-window
    # tasks — org-wide 156 is meaningless in a per-operator view.
    def _in_window(t: dict[str, Any]) -> bool:
        ts = _parse_iso(t.get("createdAt"))
        return ts is not None and from_ts <= ts <= to_ts

    if scope in (ReportScope.SELF, ReportScope.EMPLOYEE) and user_id:
        total_created = sum(
            1 for t in data.tasks
            if _in_window(t) and t.get("assigneeId") == user_id
        )
    else:
        total_created = sum(1 for t in data.tasks if _in_window(t))

    return ReportDTO(
        scope=scope,
        period_from=from_ts,
        period_to=to_ts,
        user_id=user_id,
        rows=tuple(rows),
        totals=totals,
        total_created_in_period=total_created,
    )
