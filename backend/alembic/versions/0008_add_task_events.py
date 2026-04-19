"""Add task_events append-only log for analytics

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-19

Schema mirrors Twenty's TaskLog custom object so reporting SQL doesn't
have to hit the Twenty REST API. Written in both places (Twenty TaskLog
best-effort, task_events mandatory) by WriteTaskEvent.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_events (
            event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            twenty_task_id TEXT NOT NULL,
            user_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
            location_phone TEXT,
            action TEXT NOT NULL,
            priority TEXT,
            problem_signature TEXT,
            parent_task_id TEXT,
            script_violations INTEGER,
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
        "ON task_events(twenty_task_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_events_problem_sig "
        "ON task_events(location_phone, problem_signature)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_task_events_problem_sig")
    op.execute("DROP INDEX IF EXISTS idx_task_events_twenty_task")
    op.execute("DROP INDEX IF EXISTS idx_task_events_action_created")
    op.execute("DROP INDEX IF EXISTS idx_task_events_location_created")
    op.execute("DROP INDEX IF EXISTS idx_task_events_user_created")
    op.execute("DROP TABLE IF EXISTS task_events")
