"""Initial schema from init.sql

Revision ID: 0001
Revises:
Create Date: 2026-03-27

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            chatwoot_user_id INTEGER UNIQUE NOT NULL,
            chatwoot_account_id INTEGER NOT NULL,
            role VARCHAR(20) DEFAULT 'agent' CHECK (role IN ('agent', 'supervisor', 'admin')),
            phone_internal VARCHAR(20),
            voice_sample_url VARCHAR(255),
            voice_embedding VECTOR(1536),
            settings JSONB DEFAULT '{}',
            is_active BOOLEAN DEFAULT true,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS draft_sessions (
            session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
            status VARCHAR(20) DEFAULT 'collecting'
                CHECK (status IN ('collecting', 'analyzing', 'preview', 'editing')),
            source_type VARCHAR(20) DEFAULT 'manual'
                CHECK (source_type IN ('manual', 'call_t2')),
            call_record_id UUID,
            content_blocks JSONB DEFAULT '[]',
            assembled_text TEXT,
            ai_result JSONB,
            preview_message_id INTEGER,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            expires_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() + INTERVAL '24 hours'
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS call_records (
            call_id VARCHAR(100) PRIMARY KEY,
            audio_url VARCHAR(500) NOT NULL,
            transcription_t2 TEXT,
            transcription_whisper TEXT,
            duration INTEGER,
            caller_phone VARCHAR(20),
            agent_ext VARCHAR(10),
            detected_agent_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
            voice_match_score FLOAT CHECK (voice_match_score >= 0 AND voice_match_score <= 1),
            status VARCHAR(20) DEFAULT 'new'
                CHECK (status IN ('new', 'processing', 'preview', 'created', 'error')),
            session_id UUID REFERENCES draft_sessions(session_id) ON DELETE SET NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """
    )

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
        """
        CREATE TABLE IF NOT EXISTS voice_samples (
            sample_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
            file_path VARCHAR(500) NOT NULL,
            embedding VECTOR(1536),
            duration INTEGER,
            is_active BOOLEAN DEFAULT true,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
        """
    )

    op.execute("CREATE INDEX IF NOT EXISTS idx_draft_user ON draft_sessions(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_draft_expires ON draft_sessions(expires_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_draft_status ON draft_sessions(status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_call_agent ON call_records(detected_agent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_call_status ON call_records(status)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_assignee ON task_mirrors(assignee_telegram_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_voice_user ON voice_samples(user_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_voice_active ON voice_samples(user_id, is_active)"
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_users_voice_embedding
            ON users USING hnsw (voice_embedding vector_cosine_ops)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_voice_samples_embedding
            ON voice_samples USING hnsw (embedding vector_cosine_ops)
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS update_draft_sessions_updated_at ON draft_sessions;
        CREATE TRIGGER update_draft_sessions_updated_at
            BEFORE UPDATE ON draft_sessions
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS update_draft_sessions_updated_at ON draft_sessions"
    )
    op.execute("DROP FUNCTION IF EXISTS update_updated_at_column")
    op.execute("DROP TABLE IF EXISTS voice_samples")
    op.execute("DROP TABLE IF EXISTS task_mirrors")
    op.execute("DROP TABLE IF EXISTS call_records")
    op.execute("DROP TABLE IF EXISTS draft_sessions")
    op.execute("DROP TABLE IF EXISTS users")
    op.execute("DROP EXTENSION IF EXISTS vector")
    op.execute('DROP EXTENSION IF EXISTS "uuid-ossp"')
