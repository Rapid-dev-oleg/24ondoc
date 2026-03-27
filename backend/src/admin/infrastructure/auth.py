"""Admin panel — JWT authentication utilities and FastAPI dependency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import Settings, get_settings
from telegram_ingestion.domain.models import UserRole

_ALGORITHM = "HS256"
_TOKEN_EXPIRE_HOURS = 8

bearer_scheme = HTTPBearer()


def create_access_token(telegram_id: int, role: str, secret: str) -> str:
    """Issue a JWT containing telegram_id and role claims."""
    expire = datetime.now(UTC) + timedelta(hours=_TOKEN_EXPIRE_HOURS)
    payload: dict[str, Any] = {
        "sub": str(telegram_id),
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, secret, algorithm=_ALGORITHM)


def decode_access_token(token: str, secret: str) -> dict[str, Any]:
    """Decode and validate a JWT, raise ValueError on failure."""
    try:
        return jwt.decode(token, secret, algorithms=[_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise ValueError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise ValueError("Invalid token") from exc


async def require_admin_role(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> dict[str, Any]:
    """FastAPI dependency: verify JWT and require admin or supervisor role."""
    try:
        payload = decode_access_token(credentials.credentials, settings.admin_jwt_secret)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    role = payload.get("role", "")
    if role not in (UserRole.ADMIN.value, UserRole.SUPERVISOR.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin or supervisor role",
        )
    return payload
