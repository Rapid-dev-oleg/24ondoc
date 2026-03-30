"""Application configuration via environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Database
    database_url: str
    redis_url: str = "redis://localhost:6379"

    # Telegram
    telegram_bot_token: str
    telegram_webhook_secret: str
    telegram_webhook_base_url: str = "https://24ondoc.ru"

    # OpenRouter
    openrouter_api_key: str
    openrouter_model: str = "anthropic/claude-3.5-sonnet"
    openrouter_fallback_model: str = "openai/gpt-4o"

    # OpenAI (Whisper fallback)
    openai_api_key: str = ""

    # Whisper self-hosted
    whisper_base_url: str = "http://whisper:9000"

    # T2 ATS
    t2_webhook_secret: str

    # Twenty CRM
    twenty_base_url: str = "https://24ondoc.ru"
    twenty_api_key: str = ""

    # MinIO
    minio_endpoint: str = "minio:9000"
    minio_access_key: str
    minio_secret_key: str
    minio_bucket_voices: str = "voice-samples"

    # Admin panel
    admin_jwt_secret: str = "CHANGE_ME_ADMIN_JWT_SECRET"
    admin_password: str = "CHANGE_ME_ADMIN_PASSWORD"
    env_file_path: str = ".env"
    telegram_bot_username: str = ""

    # App
    log_level: str = "INFO"
    voice_match_threshold: float = 0.85


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
