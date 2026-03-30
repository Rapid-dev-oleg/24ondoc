"""Admin panel — Pydantic schemas for request/response."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from telegram_ingestion.domain.models import UserRole


class CreateUserRequest(BaseModel):
    telegram_id: int
    name: str
    email: str
    role: UserRole = UserRole.AGENT


class UpdateUserRequest(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None
    phone_internal: str | None = None
    voice_sample_url: str | None = None
    settings: dict[str, object] | None = None


class UserResponse(BaseModel):
    telegram_id: int | None = None
    phone: str | None = None
    chatwoot_user_id: int
    chatwoot_account_id: int
    chatwoot_contact_id: int | None = None
    role: UserRole
    phone_internal: str | None = None
    voice_sample_url: str | None = None
    settings: dict[str, object] = {}
    is_active: bool | None = None
    is_pending: bool = False
    created_at: datetime


class AddPendingRequest(BaseModel):
    phone: str
    chatwoot_user_id: int
    chatwoot_account_id: int
    role: UserRole = UserRole.AGENT


class PendingUserResponse(BaseModel):
    phone: str
    chatwoot_user_id: int
    chatwoot_account_id: int
    role: UserRole
    created_at: datetime


class SettingsResponse(BaseModel):
    openrouter_api_key: str
    telegram_bot_token: str


class UpdateSettingsRequest(BaseModel):
    openrouter_api_key: str | None = None
    telegram_bot_token: str | None = None


class LoginRequest(BaseModel):
    telegram_id: int
    password: str


class TelegramAuthRequest(BaseModel):
    id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None
    auth_date: int
    hash: str


class PublicConfigResponse(BaseModel):
    telegram_bot_username: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
