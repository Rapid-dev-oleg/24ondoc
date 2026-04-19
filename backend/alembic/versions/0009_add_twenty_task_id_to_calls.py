"""Add twenty_task_id to ats_call_records

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-19

Links a local call record to the Twenty Task it produced. Populated by
CreateTwentyTaskFromSession after a Task is created from the draft
session tied to this call. Used by SyncCallToTwentyUseCase and the
backfill script to fill CallRecord.taskRelId in Twenty.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ats_call_records
            ADD COLUMN IF NOT EXISTS twenty_task_id TEXT;

        CREATE INDEX IF NOT EXISTS idx_ats_call_records_twenty_task_id
            ON ats_call_records(twenty_task_id)
            WHERE twenty_task_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS idx_ats_call_records_twenty_task_id;
        ALTER TABLE ats_call_records DROP COLUMN IF EXISTS twenty_task_id;
        """
    )
