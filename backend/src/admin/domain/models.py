"""Admin panel — Pydantic schemas for request/response."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, field_validator

from telegram_ingestion.domain.models import UserRole


class CreateUserRequest(BaseModel):
    phone: str
    name: str
    email: str
    role: UserRole = UserRole.AGENT


class UpdateUserRequest(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None


class UserResponse(BaseModel):
    telegram_id: int | None = None
    phone: str | None = None
    chatwoot_user_id: int
    chatwoot_account_id: int
    role: UserRole
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


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
