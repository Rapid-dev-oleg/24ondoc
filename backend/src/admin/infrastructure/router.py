"""Admin panel — FastAPI router for /api/admin endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse

from admin.application.use_cases import (
    GetSettingsUseCase,
    ListUsersUseCase,
    LoginWithTelegramUseCase,
    UpdateSettingsUseCase,
    UpdateUserUseCase,
)
from admin.domain.models import (
    CreateUserRequest,
    LoginRequest,
    PublicConfigResponse,
    SettingsResponse,
    TelegramAuthRequest,
    TokenResponse,
    UpdateSettingsRequest,
    UpdateUserRequest,
    UserResponse,
)
from admin.infrastructure.auth import create_access_token, require_admin_role
from admin.infrastructure.env_settings import DotEnvSettingsPort
from admin.infrastructure.telegram_notify import TelegramNotifyAdapter
from config import get_settings
from telegram_ingestion.infrastructure.user_profile_repository import (
    SQLAlchemyUserProfileRepository,
)

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

# Router without prefix — contains /admin (SPA) and full /api/admin/* paths
router = APIRouter(tags=["admin"])

AdminPayload = Annotated[dict[str, Any], Depends(require_admin_role)]


# ---------------------------------------------------------------------------
# SPA frontend
# ---------------------------------------------------------------------------


@router.get("/admin", include_in_schema=False)
async def admin_ui() -> FileResponse:
    """Serve the single-page admin panel."""
    return FileResponse(_TEMPLATES_DIR / "admin.html", media_type="text/html")


# ---------------------------------------------------------------------------
# Public config (no auth required)
# ---------------------------------------------------------------------------


@router.get("/api/admin/public-config", response_model=PublicConfigResponse)
async def public_config() -> PublicConfigResponse:
    """Return non-sensitive config for the frontend."""
    settings = get_settings()
    return PublicConfigResponse(telegram_bot_username=settings.telegram_bot_username)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@router.post("/api/admin/auth/token", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def login(body: LoginRequest, request: Request) -> TokenResponse:
    """Issue a JWT for an admin or supervisor user (password login)."""
    settings = get_settings()

    if body.password != settings.admin_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    db_session = request.state.db_session
    user_repo = SQLAlchemyUserProfileRepository(db_session)
    user = await user_repo.get_by_telegram_id(body.telegram_id)

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    if user.role.value not in ("admin", "supervisor"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    token = create_access_token(user.telegram_id, user.role.value, settings.admin_jwt_secret)
    return TokenResponse(access_token=token)


@router.post(
    "/api/admin/auth/telegram", response_model=TokenResponse, status_code=status.HTTP_200_OK
)
async def login_telegram(body: TelegramAuthRequest, request: Request) -> TokenResponse:
    """Issue a JWT after verifying Telegram Login Widget auth data."""
    settings = get_settings()
    db_session = request.state.db_session
    user_repo = SQLAlchemyUserProfileRepository(db_session)

    uc = LoginWithTelegramUseCase(
        user_repo=user_repo,
        jwt_secret=settings.admin_jwt_secret,
        bot_token=settings.telegram_bot_token,
    )
    try:
        token = await uc.execute(body)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    return TokenResponse(access_token=token)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/api/admin/users", response_model=list[UserResponse])
async def list_users(request: Request, _: AdminPayload) -> list[UserResponse]:
    db_session = request.state.db_session
    uc = ListUsersUseCase(SQLAlchemyUserProfileRepository(db_session))
    result: list[UserResponse] = await uc.execute()
    return result


@router.post("/api/admin/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(body: CreateUserRequest, request: Request, _: AdminPayload) -> UserResponse:
    settings = get_settings()
    db_session = request.state.db_session
    user_repo = SQLAlchemyUserProfileRepository(db_session)

    existing = await user_repo.get_by_telegram_id(body.telegram_id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User {body.telegram_id} already exists",
        )

    from telegram_ingestion.domain.models import UserProfile

    profile = UserProfile(telegram_id=body.telegram_id, role=body.role)
    await user_repo.save(profile)

    notify = TelegramNotifyAdapter(bot_token=settings.telegram_bot_token)
    text = (
        "✅ Вы зарегистрированы в системе 24ondoc!\n\n"
        f"Имя: {body.name}\n"
        f"Email: {body.email}\n"
        f"Роль: {body.role.value}\n\n"
        "Используйте /new_task в боте для создания задач."
    )
    await notify.send_message(body.telegram_id, text)

    return UserResponse(
        telegram_id=profile.telegram_id,
        twenty_member_id=profile.twenty_member_id,
        role=profile.role,
        phone_internal=profile.phone_internal,
        voice_sample_url=profile.voice_sample_url,
        settings=profile.settings,
        is_active=profile.is_active,
        is_pending=False,
        created_at=profile.created_at,
    )


@router.patch("/api/admin/users/{telegram_id}", response_model=UserResponse)
async def update_user(
    telegram_id: int, body: UpdateUserRequest, request: Request, _: AdminPayload
) -> UserResponse:
    db_session = request.state.db_session
    uc = UpdateUserUseCase(SQLAlchemyUserProfileRepository(db_session))
    result = await uc.execute(telegram_id, body)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return result


@router.delete("/api/admin/users/{telegram_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(telegram_id: int, request: Request, _: AdminPayload) -> None:
    db_session = request.state.db_session
    user_repo = SQLAlchemyUserProfileRepository(db_session)
    user = await user_repo.get_by_telegram_id(telegram_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await user_repo.delete_by_telegram_id(telegram_id)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/api/admin/settings", response_model=SettingsResponse)
async def get_settings_endpoint(_: AdminPayload) -> SettingsResponse:
    settings = get_settings()
    env_port = DotEnvSettingsPort(settings.env_file_path)
    return GetSettingsUseCase(env_port).execute()


@router.patch("/api/admin/settings", response_model=SettingsResponse)
async def update_settings_endpoint(
    body: UpdateSettingsRequest, _: AdminPayload
) -> SettingsResponse:
    settings = get_settings()
    env_port = DotEnvSettingsPort(settings.env_file_path)
    return UpdateSettingsUseCase(env_port).execute(body)
