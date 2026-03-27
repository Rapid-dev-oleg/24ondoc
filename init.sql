-- 24ondoc Database Initialization
-- PostgreSQL 15+ with pgvector extension

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- Пользователи (UserProfile)
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
);

-- Сессии черновиков (DraftSession, TTL 24ч)
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
);

-- Записи звонков (CallRecord)
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
);

-- Зеркало задач Chatwoot (TaskMirror)
CREATE TABLE IF NOT EXISTS task_mirrors (
    task_id INTEGER PRIMARY KEY,
    source_session_id UUID REFERENCES draft_sessions(session_id) ON DELETE SET NULL,
    assignee_telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
    status VARCHAR(20) CHECK (status IN ('open', 'pending', 'resolved', 'snoozed')),
    priority VARCHAR(20),
    title VARCHAR(255),
    permalink VARCHAR(500),
    last_sync TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Голосовые сэмплы (VoiceSample)
CREATE TABLE IF NOT EXISTS voice_samples (
    sample_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
    file_path VARCHAR(500) NOT NULL,
    embedding VECTOR(1536),
    duration INTEGER,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_draft_user ON draft_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_draft_expires ON draft_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_draft_status ON draft_sessions(status);
CREATE INDEX IF NOT EXISTS idx_call_agent ON call_records(detected_agent_id);
CREATE INDEX IF NOT EXISTS idx_call_status ON call_records(status);
CREATE INDEX IF NOT EXISTS idx_task_assignee ON task_mirrors(assignee_telegram_id);
CREATE INDEX IF NOT EXISTS idx_voice_user ON voice_samples(user_id);
CREATE INDEX IF NOT EXISTS idx_voice_active ON voice_samples(user_id, is_active);

-- Вектор-индексы для биометрии (HNSW для быстрого поиска)
CREATE INDEX IF NOT EXISTS idx_users_voice_embedding
    ON users USING hnsw (voice_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_voice_samples_embedding
    ON voice_samples USING hnsw (embedding vector_cosine_ops);

-- Функция автообновления updated_at
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_draft_sessions_updated_at
    BEFORE UPDATE ON draft_sessions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
