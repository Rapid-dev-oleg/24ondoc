"""Reports — DTOs for the per-operator report.

A report for a period is a table of rows (one per workspace member)
plus a totals row. Individual cells mirror the 12 metrics from the
plan but grouped in a way the admin can act on: who closed how many
tasks, how fast on average, how many complex ones, etc.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ReportScope(StrEnum):
    SELF = "self"          # operator looking at their own row
    OVERALL = "overall"    # admin dashboard — all operators
    EMPLOYEE = "employee"  # admin looking at a specific operator


@dataclass(frozen=True)
class EmployeeRow:
    """One row in the report table — also used for the totals footer."""

    user_id: str | None  # workspaceMemberId; None = totals / unassigned
    display_name: str    # "Надя Петрова" or "Итого"

    completed: int                            # closed in period with owner=this wm
    total_duration_seconds: int               # Σ (completion − received_at)
    avg_duration_seconds: float | None

    complex_count: int                         # subset: vazhnost ∈ {high, critical}
    avg_complex_duration_seconds: float | None

    repeats_count: int                         # closed in period + povtornoeObrashchenie
    script_violations: int                     # Σ task.scriptViolations over closed
    pending_count: int                         # snapshot: assignee=wm AND status not terminal
    avg_response_time_seconds: float | None    # avg (first_assign − created) for tasks first-assigned to wm in period


@dataclass(frozen=True)
class ReportDTO:
    scope: ReportScope
    period_from: datetime
    period_to: datetime
    user_id: str | None = None  # filter — wm id the report is focused on

    rows: tuple[EmployeeRow, ...] = ()   # per-employee rows, descending by `completed`
    totals: EmployeeRow | None = None    # footer — weighted aggregate, not avg of row averages

    total_created_in_period: int = 0     # tasks created in [from, to] — shown in header
