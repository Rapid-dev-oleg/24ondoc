"""Reports orchestrator — build a ReportDTO from the 12 metric queries.

Minimal composition layer so the Telegram bot and any future callers
don't need to know SQL. Queries all run against the same AsyncSession.
"""
from __future__ import annotations

from datetime import datetime
from statistics import mean

from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.models import ReportDTO, ReportScope
from . import queries


class GenerateReport:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        *,
        scope: ReportScope,
        from_ts: datetime,
        to_ts: datetime,
        user_id: int | None = None,
    ) -> ReportDTO:
        async with self._session_factory() as session:
            return await self._execute(session, scope, from_ts, to_ts, user_id)

    async def _execute(
        self,
        session: AsyncSession,
        scope: ReportScope,
        from_ts: datetime,
        to_ts: datetime,
        user_id: int | None,
    ) -> ReportDTO:
        # Per-user metrics
        scoped_uid = user_id if scope != ReportScope.OVERALL else None

        durations = await queries.task_durations(
            session, from_ts=from_ts, to_ts=to_ts, user_id=scoped_uid
        )
        complex_durations = await queries.task_durations(
            session, from_ts=from_ts, to_ts=to_ts, user_id=scoped_uid,
            only_high_priority=True,
        )
        completed = await queries.completed_tasks_count(
            session, from_ts=from_ts, to_ts=to_ts, user_id=scoped_uid,
        )
        script_violations = await queries.script_violations_sum(
            session, from_ts=from_ts, to_ts=to_ts, user_id=scoped_uid,
        )
        response_times = await queries.response_times(
            session, from_ts=from_ts, to_ts=to_ts, user_id=scoped_uid,
        )
        pending = await queries.pending_tasks_count(
            session, from_ts=from_ts, to_ts=to_ts, user_id=scoped_uid,
        )
        repeats_total = await queries.repeats_total(
            session, from_ts=from_ts, to_ts=to_ts, user_id=scoped_uid,
        )

        # Overall-only metrics
        total_tasks = 0
        shares: tuple = ()
        repeats_detail: tuple = ()
        if scope == ReportScope.OVERALL:
            total_tasks = await queries.total_tasks_count(
                session, from_ts=from_ts, to_ts=to_ts,
            )
            shares = tuple(
                await queries.share_per_user(session, from_ts=from_ts, to_ts=to_ts)
            )
            repeats_detail = tuple(
                await queries.repeats_by_location(session, from_ts=from_ts, to_ts=to_ts)
            )
        else:
            # Personal summary also wants total org-wide count
            total_tasks = await queries.total_tasks_count(
                session, from_ts=from_ts, to_ts=to_ts,
            )

        return ReportDTO(
            scope=scope,
            period_from=from_ts,
            period_to=to_ts,
            user_id=user_id,
            completed_tasks=completed,
            total_duration_seconds=int(sum(durations)),
            avg_duration_seconds=float(mean(durations)) if durations else None,
            complex_tasks=len(complex_durations),
            avg_complex_duration_seconds=(
                float(mean(complex_durations)) if complex_durations else None
            ),
            repeats_count=repeats_total,
            repeats_by_location=repeats_detail,
            script_violations_first_call=script_violations,
            avg_response_time_seconds=(
                float(mean(response_times)) if response_times else None
            ),
            total_tasks=total_tasks,
            share_per_user=shares,
            pending_tasks=pending,
        )
