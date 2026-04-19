"""Reports — SQL queries for each of the 12 metrics.

Each function is a small, self-contained coroutine that takes an
AsyncSession and the (from, to [, user_id]) filter. Returns raw Python
values; the orchestrator (generate_report) composes them into a ReportDTO.

Task duration model: duration = (completed_event.created_at) - (first
`created` event of the same twenty_task_id). We join task_events against
itself on twenty_task_id.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, and_, case, cast, distinct, func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from task_events.infrastructure.orm_models import TaskEventORM

from ..domain.models import EmployeeShare, LocationRepeatRow


HIGH_PRIORITIES = ("high", "urgent", "HIGH", "URGENT")


def _period(stmt: Select[Any], from_ts: datetime, to_ts: datetime) -> Select[Any]:
    return stmt.where(
        and_(TaskEventORM.created_at >= from_ts, TaskEventORM.created_at <= to_ts)
    )


async def completed_tasks_count(
    session: AsyncSession, *, from_ts: datetime, to_ts: datetime, user_id: int | None = None
) -> int:
    stmt = select(func.count(TaskEventORM.event_id)).where(
        TaskEventORM.action == "COMPLETED"
    )
    stmt = _period(stmt, from_ts, to_ts)
    if user_id is not None:
        stmt = stmt.where(TaskEventORM.user_id == user_id)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def task_durations(
    session: AsyncSession,
    *,
    from_ts: datetime,
    to_ts: datetime,
    user_id: int | None = None,
    only_high_priority: bool = False,
) -> list[float]:
    """Returns list of task durations (seconds) for tasks completed in the period.

    A task's duration = completed.created_at − earliest created-event.created_at.
    """
    created_alias = TaskEventORM.__table__.alias("c")
    completed_alias = TaskEventORM.__table__.alias("d")

    stmt = (
        select(
            (func.extract("epoch", completed_alias.c.created_at)
             - func.extract("epoch", created_alias.c.created_at)).label("dur"),
        )
        .select_from(completed_alias)
        .join(
            created_alias,
            and_(
                created_alias.c.twenty_task_id == completed_alias.c.twenty_task_id,
                created_alias.c.action == "CREATED",
            ),
        )
        .where(
            and_(
                completed_alias.c.action == "COMPLETED",
                completed_alias.c.created_at >= from_ts,
                completed_alias.c.created_at <= to_ts,
            )
        )
    )
    if user_id is not None:
        stmt = stmt.where(completed_alias.c.user_id == user_id)
    if only_high_priority:
        stmt = stmt.where(completed_alias.c.priority.in_(HIGH_PRIORITIES))

    rows = (await session.execute(stmt)).all()
    return [float(r.dur) for r in rows if r.dur is not None]


async def total_tasks_count(
    session: AsyncSession, *, from_ts: datetime, to_ts: datetime
) -> int:
    stmt = select(func.count(TaskEventORM.event_id)).where(
        TaskEventORM.action == "CREATED"
    )
    stmt = _period(stmt, from_ts, to_ts)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def pending_tasks_count(
    session: AsyncSession, *, from_ts: datetime, to_ts: datetime, user_id: int | None = None
) -> int:
    """A task is pending if it has a CREATED event in the period but no later
    COMPLETED or CANCELLED event."""
    created_ids = (
        select(TaskEventORM.twenty_task_id)
        .where(
            and_(
                TaskEventORM.action == "CREATED",
                TaskEventORM.created_at >= from_ts,
                TaskEventORM.created_at <= to_ts,
                TaskEventORM.user_id == user_id if user_id is not None else literal(True),
            )
        )
        .subquery()
    )
    terminal_ids = (
        select(TaskEventORM.twenty_task_id)
        .where(TaskEventORM.action.in_(("COMPLETED", "CANCELLED")))
        .subquery()
    )
    stmt = select(func.count(distinct(created_ids.c.twenty_task_id))).where(
        created_ids.c.twenty_task_id.notin_(select(terminal_ids.c.twenty_task_id))
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


async def script_violations_sum(
    session: AsyncSession, *, from_ts: datetime, to_ts: datetime, user_id: int | None = None
) -> int:
    stmt = select(func.coalesce(func.sum(TaskEventORM.script_violations), 0)).where(
        TaskEventORM.action == "SCRIPT_CHECKED"
    )
    stmt = _period(stmt, from_ts, to_ts)
    if user_id is not None:
        stmt = stmt.where(TaskEventORM.user_id == user_id)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def response_times(
    session: AsyncSession, *, from_ts: datetime, to_ts: datetime, user_id: int | None = None
) -> list[float]:
    """avg(first_assigned.created_at - created.created_at) in seconds."""
    created_alias = TaskEventORM.__table__.alias("c")
    assigned_alias = TaskEventORM.__table__.alias("a")

    stmt = (
        select(
            (func.min(func.extract("epoch", assigned_alias.c.created_at))
             - func.extract("epoch", created_alias.c.created_at)).label("rt"),
        )
        .select_from(created_alias)
        .join(
            assigned_alias,
            and_(
                assigned_alias.c.twenty_task_id == created_alias.c.twenty_task_id,
                assigned_alias.c.action == "ASSIGNED",
            ),
        )
        .where(
            and_(
                created_alias.c.action == "CREATED",
                created_alias.c.created_at >= from_ts,
                created_alias.c.created_at <= to_ts,
            )
        )
        .group_by(created_alias.c.twenty_task_id, created_alias.c.created_at)
    )
    if user_id is not None:
        stmt = stmt.where(assigned_alias.c.user_id == user_id)
    rows = (await session.execute(stmt)).all()
    return [float(r.rt) for r in rows if r.rt is not None]


async def repeats_by_location(
    session: AsyncSession, *, from_ts: datetime, to_ts: datetime,
) -> list[LocationRepeatRow]:
    """Count of REPEAT_CHECKED events with is_repeat=true, grouped by location."""
    stmt = (
        select(
            TaskEventORM.location_phone,
            func.count(TaskEventORM.event_id).label("c"),
        )
        .where(
            and_(
                TaskEventORM.action == "REPEAT_CHECKED",
                TaskEventORM.created_at >= from_ts,
                TaskEventORM.created_at <= to_ts,
                cast(TaskEventORM.meta["is_repeat"], Boolean).is_(True),
            )
        )
        .group_by(TaskEventORM.location_phone)
        .order_by(func.count(TaskEventORM.event_id).desc())
    )
    # NOTE: cast for JSONB bool needs a server-side cast; in unit tests we
    # won't hit postgres. Dialect-specific quirks are covered in Stage 10 E2E.
    rows = (await session.execute(stmt)).all()
    return [
        LocationRepeatRow(location_phone=r.location_phone or "", repeats=int(r.c))
        for r in rows if r.location_phone
    ]


async def repeats_total(
    session: AsyncSession, *, from_ts: datetime, to_ts: datetime, user_id: int | None = None
) -> int:
    stmt = select(func.count(TaskEventORM.event_id)).where(
        and_(
            TaskEventORM.action == "REPEAT_CHECKED",
            TaskEventORM.created_at >= from_ts,
            TaskEventORM.created_at <= to_ts,
            cast(TaskEventORM.meta["is_repeat"], None) == True,  # noqa: E712
        )
    )
    if user_id is not None:
        stmt = stmt.where(TaskEventORM.user_id == user_id)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def share_per_user(
    session: AsyncSession, *, from_ts: datetime, to_ts: datetime,
) -> list[EmployeeShare]:
    """% of completed tasks each operator did in the period."""
    stmt = (
        select(
            TaskEventORM.user_id,
            func.count(TaskEventORM.event_id).label("c"),
        )
        .where(
            and_(
                TaskEventORM.action == "COMPLETED",
                TaskEventORM.created_at >= from_ts,
                TaskEventORM.created_at <= to_ts,
                TaskEventORM.user_id.is_not(None),
            )
        )
        .group_by(TaskEventORM.user_id)
    )
    rows = (await session.execute(stmt)).all()
    total = sum(int(r.c) for r in rows)
    if total == 0:
        return []
    return [
        EmployeeShare(
            user_id=int(r.user_id),
            display_name=str(r.user_id),
            completed=int(r.c),
            share_pct=round(100.0 * int(r.c) / total, 1),
        )
        for r in rows
    ]
