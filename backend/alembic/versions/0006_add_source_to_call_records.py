"""Add source field to ats_call_records

Revision ID: 0006
Revises: 0005
Create Date: 2026-03-30

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ats_call_records
          ADD COLUMN IF NOT EXISTS source VARCHAR(50) DEFAULT 'call_t2_webhook' NOT NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE ats_call_records
          DROP COLUMN IF EXISTS source
        """
    )
