"""Reports — DTOs for the 12 metrics."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ReportScope(StrEnum):
    SELF = "self"      # operator looking at their own numbers
    OVERALL = "overall"  # admin dashboard, all operators
    EMPLOYEE = "employee"  # admin looking at a specific operator


@dataclass(frozen=True)
class EmployeeShare:
    user_id: int
    display_name: str
    completed: int
    share_pct: float  # 0..100


@dataclass(frozen=True)
class LocationRepeatRow:
    location_phone: str
    repeats: int


@dataclass(frozen=True)
class ReportDTO:
    """Flat container for all 12 metrics over a (from, to) period.

    Some fields are only populated for certain scopes (e.g.
    `share_per_user` only makes sense for OVERALL). Missing fields stay
    as empty list / 0 / None — the bot formatter shows only populated
    sections.
    """

    scope: ReportScope
    period_from: datetime
    period_to: datetime
    user_id: int | None = None

    # M1..M5 — completion timing
    completed_tasks: int = 0
    total_duration_seconds: int = 0
    avg_duration_seconds: float | None = None
    complex_tasks: int = 0
    avg_complex_duration_seconds: float | None = None

    # M6 — repeats by location
    repeats_count: int = 0
    repeats_by_location: tuple[LocationRepeatRow, ...] = ()

    # M7 — script violations at the first call per task
    script_violations_first_call: int = 0

    # M8 — response time: created → first_assigned
    avg_response_time_seconds: float | None = None

    # M9..M11 — totals
    total_tasks: int = 0
    share_per_user: tuple[EmployeeShare, ...] = ()
    pending_tasks: int = 0
