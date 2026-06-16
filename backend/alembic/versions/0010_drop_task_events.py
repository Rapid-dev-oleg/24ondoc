"""Drop task_events table — module removed

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-10

The task_events local mirror became dead code: WriteTaskEvent was never
wired up, RepeatDetection (the consumer) was superseded by DetectRepeat
which reads Twenty's built-in timelineActivity directly. The whole
backend/src/task_events/ module is being removed in this commit; this
migration drops the unused Postgres table and its indexes.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop indexes first (CASCADE on the table would also drop them, but
    # being explicit makes the intent obvious in the audit log).
    for stmt in (
        "DROP INDEX IF EXISTS idx_task_events_user_created",
        "DROP INDEX IF EXISTS idx_task_events_location_created",
        "DROP INDEX IF EXISTS idx_task_events_action_created",
        "DROP INDEX IF EXISTS idx_task_events_twenty_task",
        "DROP TABLE IF EXISTS task_events",
    ):
        op.execute(stmt)


def downgrade() -> None:
    # Recreate the schema from 0008 in case of revert. Data is gone.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_events (
            id BIGSERIAL PRIMARY KEY,
            twenty_task_id TEXT NOT NULL,
            user_id BIGINT,
            location_phone TEXT,
            action TEXT NOT NULL,
            priority TEXT,
            problem_signature TEXT,
            parent_task_id TEXT,
            script_violations INT,
            script_missing JSONB,
            source TEXT,
            meta JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_events_user_created "
        "ON task_events(user_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_events_location_created "
        "ON task_events(location_phone, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_events_action_created "
        "ON task_events(action, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_events_twenty_task "
        "ON task_events(twenty_task_id, created_at DESC)"
    )
