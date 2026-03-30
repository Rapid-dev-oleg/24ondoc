"""Remove Chatwoot fields from users, add twenty_member_id

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-30

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE users
          DROP COLUMN IF EXISTS chatwoot_user_id,
          DROP COLUMN IF EXISTS chatwoot_account_id,
          DROP COLUMN IF EXISTS chatwoot_contact_id,
          ADD COLUMN IF NOT EXISTS twenty_member_id VARCHAR(36) NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE users
          DROP COLUMN IF EXISTS twenty_member_id,
          ADD COLUMN IF NOT EXISTS chatwoot_user_id INTEGER,
          ADD COLUMN IF NOT EXISTS chatwoot_account_id INTEGER,
          ADD COLUMN IF NOT EXISTS chatwoot_contact_id INTEGER NULL
        """
    )
