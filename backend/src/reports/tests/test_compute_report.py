"""Unit tests for reports.compute_report.

Synthetic TimelineData → ReportDTO. Verifies per-operator aggregation,
duration from `received_at`, the totals footer, and edge cases.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reports.application.compute_report import compute_report
from reports.domain.models import ReportScope
from reports.infrastructure.twenty_timeline_reader import TimelineData


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _tu_event(tid: str, happens_at: datetime, diff: dict, wmid: str | None = None) -> dict:
    return {
        "targetTaskId": tid,
        "happensAt": _iso(happens_at),
        "createdAt": _iso(happens_at),
        "workspaceMemberId": wmid,
        "properties": {"diff": diff},
    }


def _tc_event(tid: str, happens_at: datetime) -> dict:
    """task.created timeline event — carries the real Twenty INSERT timestamp."""
    return {
        "targetTaskId": tid,
        "name": "task.created",
        "happensAt": _iso(happens_at),
        "createdAt": _iso(happens_at),
        "workspaceMemberId": None,
        "properties": {},
    }


WM_NADYA = "wm-nadya"
WM_VOVA = "wm-vova"


def test_single_completion_with_reassignment() -> None:
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)

    t_created = datetime(2026, 4, 10, 10, 0, tzinfo=UTC)
    t_received = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)   # Vova picks it up
    t_completed = datetime(2026, 4, 11, 13, 0, tzinfo=UTC)  # 1h later

    tasks = ({
        "id": "t1",
        "createdAt": _iso(t_created),
        "assigneeId": WM_VOVA,
        "status": "VYPOLNENO",
        "vazhnost": "SREDNYAYA",
        "povtornoeObrashchenie": False,
        "scriptViolations": None,
    },)
    events = (
        _tu_event("t1", t_received, {"assigneeId": {"before": None, "after": WM_VOVA}}),
        _tu_event("t1", t_completed, {"status": {"before": "V_RABOTE", "after": "VYPOLNENO"}}, wmid=WM_VOVA),
    )
    data = TimelineData(
        updated_events=events, tasks=tasks,
        members_by_id={WM_VOVA: "Вова Петров"},
    )
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)

    assert dto.totals is not None
    assert dto.totals.completed == 1
    assert dto.totals.total_duration_seconds == 3600
    # Should attribute to Vova, with 1h duration from received (NOT creation)
    rows = {r.user_id: r for r in dto.rows}
    assert WM_VOVA in rows
    assert rows[WM_VOVA].completed == 1
    assert rows[WM_VOVA].total_duration_seconds == 3600
    assert rows[WM_VOVA].display_name == "Вова Петров"


def test_completion_without_assignment_event_excluded() -> None:
    """No assignment event in timeline → task excluded from duration metrics.

    task.createdAt is NOT used as a fallback: our backend backfills it to
    the ATS call time (hours/days before the real Twenty INSERT), so if
    we trusted it we'd report multi-hour durations for batch-closed tasks
    that took zero real work. Script-violations still count — they come
    from the task snapshot, not the timeline.
    """
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)

    t_created = datetime(2026, 4, 5, 9, 0, tzinfo=UTC)
    t_completed = datetime(2026, 4, 5, 10, 30, tzinfo=UTC)

    tasks = ({
        "id": "t2", "createdAt": _iso(t_created),
        "assigneeId": WM_NADYA, "status": "VYPOLNENO",
        "vazhnost": "VYSOKAYA", "scriptViolations": 2,
    },)
    events = (
        _tu_event("t2", t_completed, {"status": {"before": "TODO", "after": "VYPOLNENO"}}, wmid=WM_NADYA),
    )
    data = TimelineData(events, tasks, members_by_id={WM_NADYA: "Надя"})

    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    row = next(r for r in dto.rows if r.user_id == WM_NADYA)
    assert row.completed == 0
    assert row.total_duration_seconds == 0
    assert row.avg_duration_seconds is None
    assert row.complex_count == 0
    # M7 (intake-side metric) still counts from the task snapshot.
    assert row.script_violations == 2


def test_completion_without_status_event_excluded() -> None:
    """Task in VYPOLNENO state but timeline has NO status→VYPOLNENO event → skip.

    Covers legacy tasks where the status was written directly to DB without
    emitting a timelineActivity. Inflated durations come from pretending we
    closed such tasks "just now" — refuse to guess.
    """
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)

    t_created = datetime(2026, 4, 5, 9, 0, tzinfo=UTC)
    tasks = ({
        "id": "t3", "createdAt": _iso(t_created),
        "assigneeId": WM_NADYA, "status": "VYPOLNENO",
    },)
    # Only an assignment event, NO status transition.
    events = (
        _tu_event("t3", t_created + timedelta(minutes=10),
                  {"assigneeId": {"before": None, "after": WM_NADYA}}),
    )
    data = TimelineData(events, tasks, members_by_id={WM_NADYA: "Надя"})
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    assert dto.totals is not None
    assert dto.totals.completed == 0


def test_totals_are_weighted_not_mean_of_means() -> None:
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)
    base = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)

    # Each task: assignment at `base`, completion N seconds later.
    # Vova: 1 task × 60s; Nadya: 3 × 1200s → total 3660s; weighted avg = 915.
    tasks = (
        {"id": "v", "createdAt": _iso(base), "assigneeId": WM_VOVA, "status": "VYPOLNENO"},
        {"id": "n1", "createdAt": _iso(base), "assigneeId": WM_NADYA, "status": "VYPOLNENO"},
        {"id": "n2", "createdAt": _iso(base), "assigneeId": WM_NADYA, "status": "VYPOLNENO"},
        {"id": "n3", "createdAt": _iso(base), "assigneeId": WM_NADYA, "status": "VYPOLNENO"},
    )
    events = (
        _tu_event("v",  base, {"assigneeId": {"before": None, "after": WM_VOVA}}),
        _tu_event("n1", base, {"assigneeId": {"before": None, "after": WM_NADYA}}),
        _tu_event("n2", base, {"assigneeId": {"before": None, "after": WM_NADYA}}),
        _tu_event("n3", base, {"assigneeId": {"before": None, "after": WM_NADYA}}),
        _tu_event("v",  base + timedelta(seconds=60),   {"status": {"before": "TODO", "after": "VYPOLNENO"}}),
        _tu_event("n1", base + timedelta(seconds=1200), {"status": {"before": "TODO", "after": "VYPOLNENO"}}),
        _tu_event("n2", base + timedelta(seconds=1200), {"status": {"before": "TODO", "after": "VYPOLNENO"}}),
        _tu_event("n3", base + timedelta(seconds=1200), {"status": {"before": "TODO", "after": "VYPOLNENO"}}),
    )
    data = TimelineData(events, tasks, members_by_id={WM_VOVA: "Vova", WM_NADYA: "Nadya"})
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    assert dto.totals is not None
    assert dto.totals.completed == 4
    assert dto.totals.total_duration_seconds == 3660
    assert dto.totals.avg_duration_seconds == 915.0  # weighted, not (60+1200)/2


def test_completion_outside_window_excluded() -> None:
    from_ts = datetime(2026, 4, 10, tzinfo=UTC)
    to_ts = datetime(2026, 4, 20, tzinfo=UTC)

    t_before = datetime(2026, 4, 5, tzinfo=UTC)  # before window
    tasks = ({"id": "t", "createdAt": _iso(t_before), "assigneeId": WM_VOVA,
              "status": "VYPOLNENO"},)
    events = (
        _tu_event("t", t_before + timedelta(hours=1),
                  {"status": {"before": "TODO", "after": "VYPOLNENO"}}),
    )
    data = TimelineData(events, tasks, members_by_id={})
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    assert dto.totals is not None
    assert dto.totals.completed == 0


def test_pending_snapshot_counts_current_assignees() -> None:
    # Pending is not window-scoped: we just count tasks whose status is not terminal.
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)
    tasks = (
        {"id": "a", "createdAt": _iso(from_ts), "assigneeId": WM_VOVA, "status": "V_RABOTE"},
        {"id": "b", "createdAt": _iso(from_ts), "assigneeId": WM_NADYA, "status": "TODO"},
        {"id": "c", "createdAt": _iso(from_ts), "assigneeId": WM_NADYA, "status": "VYPOLNENO"},
    )
    data = TimelineData((), tasks, members_by_id={WM_VOVA: "V", WM_NADYA: "N"})
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    by_user = {r.user_id: r.pending_count for r in dto.rows}
    assert by_user[WM_VOVA] == 1
    assert by_user[WM_NADYA] == 1  # only the non-terminal one
    assert dto.totals is not None
    assert dto.totals.pending_count == 2


def test_scope_self_filters_rows() -> None:
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)
    tasks = (
        {"id": "a", "createdAt": _iso(from_ts), "assigneeId": WM_VOVA, "status": "VYPOLNENO"},
        {"id": "b", "createdAt": _iso(from_ts), "assigneeId": WM_NADYA, "status": "VYPOLNENO"},
    )
    events = (
        _tu_event("a", from_ts, {"assigneeId": {"before": None, "after": WM_VOVA}}),
        _tu_event("b", from_ts, {"assigneeId": {"before": None, "after": WM_NADYA}}),
        _tu_event("a", from_ts + timedelta(hours=1),
                  {"status": {"before": "TODO", "after": "VYPOLNENO"}}),
        _tu_event("b", from_ts + timedelta(hours=2),
                  {"status": {"before": "TODO", "after": "VYPOLNENO"}}),
    )
    data = TimelineData(events, tasks, members_by_id={WM_VOVA: "V", WM_NADYA: "N"})
    dto = compute_report(
        data, from_ts=from_ts, to_ts=to_ts,
        scope=ReportScope.SELF, user_id=WM_VOVA,
    )
    assert len(dto.rows) == 1
    assert dto.rows[0].user_id == WM_VOVA
    # totals still reflect the whole org, not the filtered row
    assert dto.totals is not None
    assert dto.totals.completed == 2


def test_response_time_uses_task_created_event_not_column() -> None:
    """M8 measures first-assignment − task.created event, NOT − task.createdAt.

    Our backend backfills task.createdAt to the ATS call time (days before
    the real Twenty INSERT), so using the column would conflate CRM-entry
    delay with operator reaction. Task.created timeline event is the
    trustworthy anchor.
    """
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)

    # Column says 04-05 (call date), but Twenty actually inserted 04-10;
    # assignee picked it up 5 minutes after the real insert.
    backfilled_created = datetime(2026, 4, 5, 9, 0, tzinfo=UTC)
    real_insert = datetime(2026, 4, 10, 14, 0, tzinfo=UTC)
    first_assign = real_insert + timedelta(minutes=5)

    tasks = ({
        "id": "t", "createdAt": _iso(backfilled_created),
        "assigneeId": WM_VOVA, "status": "TODO",
    },)
    updated = (_tu_event("t", first_assign,
                         {"assigneeId": {"before": None, "after": WM_VOVA}}),)
    created = (_tc_event("t", real_insert),)
    data = TimelineData(updated, tasks, members_by_id={WM_VOVA: "V"},
                        created_events=created)
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts)
    row = next(r for r in dto.rows if r.user_id == WM_VOVA)
    # 5 minutes, NOT 5 days + 5 minutes
    assert row.avg_response_time_seconds == 5 * 60


def test_response_time_task_without_created_event_skipped() -> None:
    """No task.created in timeline → response time can't be trusted → skip."""
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)

    tasks = ({
        "id": "t", "createdAt": _iso(from_ts),
        "assigneeId": WM_VOVA, "status": "TODO",
    },)
    updated = (_tu_event("t", from_ts + timedelta(minutes=10),
                         {"assigneeId": {"before": None, "after": WM_VOVA}}),)
    data = TimelineData(updated, tasks, members_by_id={WM_VOVA: "V"})
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts)
    row = next(r for r in dto.rows if r.user_id == WM_VOVA)
    assert row.avg_response_time_seconds is None
