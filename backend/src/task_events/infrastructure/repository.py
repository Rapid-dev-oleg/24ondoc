"""Task Events — SQLAlchemy repository."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.models import Action, Source, TaskEvent
from ..domain.ports import TaskEventRepository
from .orm_models import TaskEventORM


class SQLTaskEventRepository(TaskEventRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: TaskEvent) -> None:
        row = TaskEventORM(
            event_id=event.event_id,
            twenty_task_id=event.twenty_task_id,
            user_id=event.user_id,
            location_phone=event.location_phone,
            action=event.action.value,
            priority=event.priority,
            problem_signature=event.problem_signature,
            parent_task_id=event.parent_task_id,
            script_violations=event.script_violations,
            script_missing=event.script_missing,
            source=event.source.value if event.source else None,
            meta=event.meta,
            created_at=event.created_at,
        )
        self._session.add(row)
        await self._session.flush()

    async def find_recent_by_location(
        self,
        location_phone: str,
        *,
        since: datetime,
        limit: int = 10,
        action: Action = Action.CREATED,
    ) -> list[TaskEvent]:
        stmt = (
            select(TaskEventORM)
            .where(
                and_(
                    TaskEventORM.location_phone == location_phone,
                    TaskEventORM.created_at >= since,
                    TaskEventORM.action == action.value,
                )
            )
            .order_by(TaskEventORM.created_at.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(r) for r in rows]

    async def has_action_for_task(
        self, twenty_task_id: str, action: Action
    ) -> bool:
        stmt = (
            select(TaskEventORM.event_id)
            .where(
                and_(
                    TaskEventORM.twenty_task_id == twenty_task_id,
                    TaskEventORM.action == action.value,
                )
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def recent_by_task(
        self, twenty_task_id: str, limit: int = 20
    ) -> list[TaskEvent]:
        stmt = (
            select(TaskEventORM)
            .where(TaskEventORM.twenty_task_id == twenty_task_id)
            .order_by(TaskEventORM.created_at.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(r) for r in rows]


def _to_domain(row: TaskEventORM) -> TaskEvent:
    return TaskEvent(
        event_id=row.event_id,
        twenty_task_id=row.twenty_task_id,
        user_id=row.user_id,
        location_phone=row.location_phone,
        action=Action(row.action),
        priority=row.priority,
        problem_signature=row.problem_signature,
        parent_task_id=row.parent_task_id,
        script_violations=row.script_violations,
        script_missing=row.script_missing,
        source=Source(row.source) if row.source else None,
        meta=row.meta,
        created_at=row.created_at,
    )
