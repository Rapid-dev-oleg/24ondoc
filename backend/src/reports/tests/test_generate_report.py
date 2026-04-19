"""Stage 8 — GenerateReport orchestrator tests (queries module mocked).

We don't hit postgres here — the actual SQL correctness is covered by
the Stage 10 E2E run. This test verifies the composition: which
queries get called for which scope, and how values flow into the DTO.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.reports.application.generate_report import GenerateReport
from src.reports.domain.models import EmployeeShare, LocationRepeatRow, ReportScope


class _FakeSessionCtx:
    """Minimal async-context-manager yielding a MagicMock session."""

    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *_):
        return None


def _factory() -> MagicMock:
    f = MagicMock()
    f.side_effect = lambda: _FakeSessionCtx()
    return f


def _from_to() -> tuple[datetime, datetime]:
    return datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 4, 19, tzinfo=UTC)


@pytest.mark.asyncio
async def test_self_scope_skips_overall_only_queries() -> None:
    from_ts, to_ts = _from_to()
    uc = GenerateReport(session_factory=_factory())

    with patch("src.reports.application.generate_report.queries") as q:
        q.task_durations = AsyncMock(side_effect=[[60.0, 120.0], [300.0]])
        q.completed_tasks_count = AsyncMock(return_value=2)
        q.script_violations_sum = AsyncMock(return_value=1)
        q.response_times = AsyncMock(return_value=[15.0, 25.0])
        q.pending_tasks_count = AsyncMock(return_value=3)
        q.repeats_total = AsyncMock(return_value=4)
        q.total_tasks_count = AsyncMock(return_value=100)
        q.share_per_user = AsyncMock()
        q.repeats_by_location = AsyncMock()

        dto = await uc.execute(scope=ReportScope.SELF, from_ts=from_ts, to_ts=to_ts, user_id=7)

        q.share_per_user.assert_not_called()
        q.repeats_by_location.assert_not_called()

    assert dto.scope == ReportScope.SELF
    assert dto.completed_tasks == 2
    assert dto.total_duration_seconds == 180  # 60+120
    assert dto.avg_duration_seconds == 90.0
    assert dto.complex_tasks == 1
    assert dto.avg_complex_duration_seconds == 300.0
    assert dto.repeats_count == 4
    assert dto.pending_tasks == 3
    assert dto.script_violations_first_call == 1
    assert dto.avg_response_time_seconds == 20.0
    assert dto.total_tasks == 100  # personal summary includes org-wide


@pytest.mark.asyncio
async def test_overall_scope_calls_share_and_repeats_detail() -> None:
    from_ts, to_ts = _from_to()
    uc = GenerateReport(session_factory=_factory())

    with patch("src.reports.application.generate_report.queries") as q:
        q.task_durations = AsyncMock(return_value=[])
        q.completed_tasks_count = AsyncMock(return_value=0)
        q.script_violations_sum = AsyncMock(return_value=0)
        q.response_times = AsyncMock(return_value=[])
        q.pending_tasks_count = AsyncMock(return_value=0)
        q.repeats_total = AsyncMock(return_value=0)
        q.total_tasks_count = AsyncMock(return_value=42)
        q.share_per_user = AsyncMock(
            return_value=[
                EmployeeShare(user_id=1, display_name="1", completed=4, share_pct=40.0),
                EmployeeShare(user_id=2, display_name="2", completed=6, share_pct=60.0),
            ]
        )
        q.repeats_by_location = AsyncMock(
            return_value=[LocationRepeatRow(location_phone="79000000000", repeats=3)]
        )

        dto = await uc.execute(scope=ReportScope.OVERALL, from_ts=from_ts, to_ts=to_ts)

    assert dto.total_tasks == 42
    assert len(dto.share_per_user) == 2
    assert len(dto.repeats_by_location) == 1
    assert dto.repeats_by_location[0].location_phone == "79000000000"


@pytest.mark.asyncio
async def test_empty_durations_gives_none_averages() -> None:
    from_ts, to_ts = _from_to()
    uc = GenerateReport(session_factory=_factory())

    with patch("src.reports.application.generate_report.queries") as q:
        q.task_durations = AsyncMock(return_value=[])
        q.completed_tasks_count = AsyncMock(return_value=0)
        q.script_violations_sum = AsyncMock(return_value=0)
        q.response_times = AsyncMock(return_value=[])
        q.pending_tasks_count = AsyncMock(return_value=0)
        q.repeats_total = AsyncMock(return_value=0)
        q.total_tasks_count = AsyncMock(return_value=0)
        q.share_per_user = AsyncMock(return_value=[])
        q.repeats_by_location = AsyncMock(return_value=[])

        dto = await uc.execute(scope=ReportScope.SELF, from_ts=from_ts, to_ts=to_ts, user_id=1)

    assert dto.avg_duration_seconds is None
    assert dto.avg_complex_duration_seconds is None
    assert dto.avg_response_time_seconds is None
