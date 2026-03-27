"""Admin panel — FastAPI router for /api/admin endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse

from admin.application.use_cases import (
    AddPendingUseCase,
    CreateOperatorUseCase,
    DeactivateUserUseCase,
    DeletePendingUseCase,
    GetSettingsUseCase,
    ListPendingUseCase,
    ListUsersUseCase,
    LoginWithTelegramUseCase,
    UpdateSettingsUseCase,
    UpdateUserUseCase,
)
from admin.domain.models import (
    AddPendingRequest,
    CreateUserRequest,
    LoginRequest,
    PendingUserResponse,
    PublicConfigResponse,
    SettingsResponse,
    TelegramAuthRequest,
    TokenResponse,
    UpdateSettingsRequest,
    UpdateUserRequest,
    UserResponse,
)
from admin.infrastructure.auth import create_access_token, require_admin_role
from admin.infrastructure.chatwoot_admin_client import ChatwootAdminClient
from admin.infrastructure.env_settings import DotEnvSettingsPort
from config import get_settings
from telegram_ingestion.infrastructure.pending_user_repository import (
    SQLAlchemyPendingUserRepository,
)
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
    uc = ListUsersUseCase(
        SQLAlchemyUserProfileRepository(db_session),
        SQLAlchemyPendingUserRepository(db_session),
    )
    result: list[UserResponse] = await uc.execute()
    return result


@router.post(
    "/api/admin/users", response_model=PendingUserResponse, status_code=status.HTTP_201_CREATED
)
async def create_user(
    body: CreateUserRequest, request: Request, _: AdminPayload
) -> PendingUserResponse:
    settings = get_settings()
    db_session = request.state.db_session
    chatwoot = ChatwootAdminClient(
        base_url=settings.chatwoot_base_url,
        api_key=settings.chatwoot_api_key,
        account_id=settings.chatwoot_account_id,
    )
    uc = CreateOperatorUseCase(
        chatwoot=chatwoot,
        pending_repo=SQLAlchemyPendingUserRepository(db_session),
        account_id=settings.chatwoot_account_id,
    )
    try:
        return await uc.execute(body)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


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
async def deactivate_user(telegram_id: int, request: Request, _: AdminPayload) -> None:
    db_session = request.state.db_session
    uc = DeactivateUserUseCase(SQLAlchemyUserProfileRepository(db_session))
    found = await uc.execute(telegram_id)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")


# ---------------------------------------------------------------------------
# Pending users
# ---------------------------------------------------------------------------


@router.get("/api/admin/pending", response_model=list[PendingUserResponse])
async def list_pending(request: Request, _: AdminPayload) -> list[PendingUserResponse]:
    db_session = request.state.db_session
    uc = ListPendingUseCase(SQLAlchemyPendingUserRepository(db_session))
    result: list[PendingUserResponse] = await uc.execute()
    return result


@router.post(
    "/api/admin/pending",
    response_model=PendingUserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_pending(
    body: AddPendingRequest, request: Request, _: AdminPayload
) -> PendingUserResponse:
    db_session = request.state.db_session
    uc = AddPendingUseCase(SQLAlchemyPendingUserRepository(db_session))
    return await uc.execute(body)


@router.delete("/api/admin/pending/{phone}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pending(phone: str, request: Request, _: AdminPayload) -> None:
    db_session = request.state.db_session
    uc = DeletePendingUseCase(SQLAlchemyPendingUserRepository(db_session))
    found = await uc.execute(phone)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pending user not found")


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
