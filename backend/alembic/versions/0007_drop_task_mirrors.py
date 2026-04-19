"""Drop legacy task_mirrors table (Chatwoot era, unused after Twenty migration)

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-19

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_task_assignee")
    op.execute("DROP TABLE IF EXISTS task_mirrors")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_mirrors (
            task_id INTEGER PRIMARY KEY,
            source_session_id UUID REFERENCES draft_sessions(session_id) ON DELETE SET NULL,
            assignee_telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
            status VARCHAR(20) CHECK (status IN ('open', 'pending', 'resolved', 'snoozed')),
            priority VARCHAR(20),
            title VARCHAR(255),
            permalink VARCHAR(500),
            last_sync TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_assignee ON task_mirrors(assignee_telegram_id)"
    )
