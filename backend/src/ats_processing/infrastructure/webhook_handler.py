"""ATS Processing — T2 Webhook endpoint."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from pydantic import BaseModel

from ..domain.models import CallRecord
from ..domain.repository import CallRecordRepository

logger = logging.getLogger(__name__)

router = APIRouter()


class T2CallPayload(BaseModel):
    """Pydantic схема входящего webhook-payload от АТС Т2."""

    call_id: str
    audio_url: str
    caller_phone: str
    agent_ext: str
    transcription_t2: str | None = None
    duration: int | None = None


def _get_call_repo(request: Request) -> CallRecordRepository:
    repo: CallRecordRepository | None = getattr(request.state, "call_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CallRecordRepository not available",
        )
    return repo


def _get_process_fn(request: Request) -> Any:
    """Получить фоновую функцию обработки звонка из request.state."""
    return getattr(request.state, "process_call_fn", None)


@router.post("/webhook/t2/call", status_code=status.HTTP_200_OK)
async def t2_call_webhook(
    request: Request,
    payload: T2CallPayload,
    background_tasks: BackgroundTasks,
    x_t2_secret: str | None = Header(default=None, alias="X-T2-Secret"),
) -> dict[str, str]:
    """Webhook endpoint для получения звонков от АТС Т2.

    Валидирует секрет, создаёт CallRecord, запускает background обработку.
    """
    # Validate secret
    expected_secret: str | None = getattr(request.state, "t2_webhook_secret", None)
    if expected_secret is None or x_t2_secret != expected_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-T2-Secret header",
        )

    call_repo = _get_call_repo(request)

    # Create CallRecord
    call_record = CallRecord(
        call_id=payload.call_id,
        audio_url=payload.audio_url,
        caller_phone=payload.caller_phone,
        agent_ext=payload.agent_ext,
        duration=payload.duration,
    )
    if payload.transcription_t2:
        call_record.set_transcription(payload.transcription_t2, source="t2")

    await call_repo.save(call_record)
    logger.info("CallRecord created: %s", payload.call_id)

    # Schedule background processing
    process_fn = _get_process_fn(request)
    if process_fn is not None:
        background_tasks.add_task(process_fn, payload.call_id)

    return {"status": "accepted", "call_id": payload.call_id}
