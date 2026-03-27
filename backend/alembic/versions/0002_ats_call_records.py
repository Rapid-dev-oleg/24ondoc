"""ATS call records table

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-27

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ats_call_records (
            call_id VARCHAR(100) PRIMARY KEY,
            audio_url VARCHAR(500) NOT NULL,
            transcription_t2 TEXT,
            transcription_whisper TEXT,
            duration INTEGER,
            caller_phone VARCHAR(20),
            agent_ext VARCHAR(10),
            detected_agent_id INTEGER,
            voice_match_score FLOAT CHECK (voice_match_score >= 0 AND voice_match_score <= 1),
            status VARCHAR(20) DEFAULT 'new'
                CHECK (status IN ('new', 'processing', 'preview', 'created', 'error')),
            session_id UUID,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_ats_call_records_caller_phone"
        " ON ats_call_records (caller_phone, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_ats_call_records_status"
        " ON ats_call_records (status)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ats_call_records")
