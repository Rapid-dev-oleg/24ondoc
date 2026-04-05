"""Application configuration via environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str
    redis_url: str = "redis://localhost:6379"

    # Telegram
    telegram_bot_token: str
    telegram_webhook_secret: str
    telegram_webhook_base_url: str = "https://24ondoc.ru"

    # OpenRouter
    openrouter_api_key: str
    openrouter_model: str = "anthropic/claude-sonnet-4"
    openrouter_fallback_model: str = "anthropic/claude-3.7-sonnet"

    # OpenAI (Whisper fallback)
    openai_api_key: str = ""

    # Whisper self-hosted
    whisper_base_url: str = "http://whisper:9000"

    # Groq (Whisper transcription)
    groq_api_key: str = ""

    # T2 ATS
    t2_webhook_secret: str

    # ATS2 REST API
    ats2_base_url: str = "https://ats2.t2.ru/crm/openapi"
    ats2_access_token: str = ""
    ats2_refresh_token: str = ""
    ats2_poll_interval_sec: int = 60
    ats2_enabled: bool = False
    ats2_proxy_url: str = ""

    # Twenty CRM
    twenty_base_url: str = "https://24ondoc.ru"
    twenty_api_key: str = ""

    # MinIO
    minio_endpoint: str = "minio:9000"
    minio_access_key: str
    minio_secret_key: str
    minio_bucket_voices: str = "voice-samples"

    # Admin panel
    admin_jwt_secret: str
    admin_password: str
    env_file_path: str = ".env"
    telegram_bot_username: str = ""

    # App
    log_level: str = "INFO"
    voice_match_threshold: float = 0.85


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
