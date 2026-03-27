"""Telegram Ingestion — FastAPI webhook endpoint bridging to aiogram Dispatcher."""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Update
from fastapi import APIRouter, Header, HTTPException, Request, status

from ats_processing.infrastructure.repository import CallRecordRepositoryImpl
from chatwoot_integration.infrastructure.chatwoot_client import ChatwootClient
from telegram_ingestion.application.tasks_use_cases import (
    AddTaskCommentUseCase,
    GetMyTasksUseCase,
    ReassignTaskUseCase,
    UpdateTaskStatusUseCase,
)
from telegram_ingestion.application.use_cases import (
    AddTextContentUseCase,
    AddVoiceContentUseCase,
    CancelSessionUseCase,
    StartSessionUseCase,
    TriggerAnalysisUseCase,
)
from telegram_ingestion.infrastructure.bot_handler import (
    create_call_notification_router,
    create_router,
    create_tasks_router,
)
from telegram_ingestion.infrastructure.draft_session_repository import (
    SQLAlchemyRedisDraftSessionRepository,
)
from telegram_ingestion.infrastructure.user_profile_port import UserProfilePortAdapter
from telegram_ingestion.infrastructure.user_profile_repository import (
    SQLAlchemyUserProfileRepository,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhook/telegram", status_code=status.HTTP_200_OK)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, str]:
    """Принимает Telegram Update и обрабатывает через aiogram Dispatcher.

    Для каждого запроса создаются свежие репозитории с per-request DB-сессией.
    FSM-состояния хранятся в Redis (персистентно между вебхук-вызовами).
    """
    settings = request.app.state.settings

    # Validate Telegram webhook secret
    if (
        settings.telegram_webhook_secret
        and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Telegram webhook secret",
        )

    update_data = await request.json()

    bot: Bot = request.app.state.bot
    redis = request.app.state.redis
    chatwoot_client: ChatwootClient = request.app.state.chatwoot_client
    stt_port = request.app.state.stt_port
    db_session = request.state.db_session

    # Per-request repos
    draft_repo = SQLAlchemyRedisDraftSessionRepository(db_session, redis)
    user_repo = SQLAlchemyUserProfileRepository(db_session)
    user_port = UserProfilePortAdapter(user_repo)
    call_repo = CallRecordRepositoryImpl(db_session)

    # Per-request use cases
    start_session = StartSessionUseCase(draft_repo, user_port)
    add_text = AddTextContentUseCase(draft_repo)
    add_voice = AddVoiceContentUseCase(draft_repo, stt_port)
    trigger_analysis = TriggerAnalysisUseCase(draft_repo)
    cancel_session = CancelSessionUseCase(draft_repo)

    get_my_tasks = GetMyTasksUseCase(user_port, chatwoot_client)
    update_task_status = UpdateTaskStatusUseCase(user_port, chatwoot_client)
    reassign_task = ReassignTaskUseCase(user_port, chatwoot_client)
    add_task_comment = AddTaskCommentUseCase(chatwoot_client)

    # Dispatcher with Redis FSM storage (shared state across per-request instances)
    storage = RedisStorage(redis=redis)
    dp = Dispatcher(storage=storage)
    dp.include_router(
        create_router(start_session, add_text, add_voice, trigger_analysis, cancel_session)
    )
    dp.include_router(
        create_tasks_router(
            get_my_tasks, update_task_status, reassign_task, add_task_comment, user_port
        )
    )
    dp.include_router(create_call_notification_router(call_repo))

    update = Update.model_validate(update_data)
    await dp.feed_update(bot, update)

    return {"ok": "true"}
