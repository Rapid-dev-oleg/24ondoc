"""Add pending_users table

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-28

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_users (
            phone VARCHAR(20) PRIMARY KEY,
            chatwoot_user_id INTEGER UNIQUE NOT NULL,
            chatwoot_account_id INTEGER NOT NULL,
            role VARCHAR(20) DEFAULT 'agent' CHECK (role IN ('agent', 'supervisor', 'admin')),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pending_users")
