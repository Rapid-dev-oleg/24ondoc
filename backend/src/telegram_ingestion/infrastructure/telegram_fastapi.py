"""Telegram Ingestion — FastAPI webhook endpoint bridging to aiogram Dispatcher."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import ErrorEvent, Update
from fastapi import APIRouter, Header, HTTPException, Request, status

from ai_classification.infrastructure.openrouter_adapter import OpenRouterAdapter
from ats_processing.infrastructure.repository import CallRecordRepositoryImpl
from telegram_ingestion.application.registration_use_cases import (
    AutoRegisterUserUseCase,
    SaveVoiceSampleUseCase,
    UpdateProfileFieldUseCase,
)
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
    SetAnalysisResultUseCase,
    StartSessionUseCase,
    TriggerAnalysisUseCase,
)
from telegram_ingestion.infrastructure.bot_handler import (
    create_call_notification_router,
    create_router,
    create_settings_router,
    create_tasks_router,
)
from telegram_ingestion.infrastructure.draft_session_repository import (
    SQLAlchemyRedisDraftSessionRepository,
)
from telegram_ingestion.infrastructure.local_voice_storage import LocalVoiceSampleStorage
from telegram_ingestion.infrastructure.user_profile_port import UserProfilePortAdapter
from telegram_ingestion.infrastructure.user_profile_repository import (
    SQLAlchemyUserProfileRepository,
)
from twenty_integration.application.use_cases import CreateTwentyTaskFromSession
from twenty_integration.infrastructure.twenty_adapter import TwentyRestAdapter

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
    twenty_adapter: TwentyRestAdapter = request.app.state.twenty_adapter
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
    set_analysis_result = SetAnalysisResultUseCase(draft_repo)

    get_my_tasks = GetMyTasksUseCase(user_port, twenty_adapter)
    update_task_status = UpdateTaskStatusUseCase(user_port, twenty_adapter)
    reassign_task = ReassignTaskUseCase(user_port, twenty_adapter)
    add_task_comment = AddTaskCommentUseCase(twenty_adapter)

    # Registration + profile use cases
    auto_register = AutoRegisterUserUseCase(user_repo)
    update_profile = UpdateProfileFieldUseCase(user_repo)
    voice_storage = LocalVoiceSampleStorage(base_dir="/data/voice_samples")
    save_voice = SaveVoiceSampleUseCase(user_repo, voice_storage)

    # AI classification + Twenty CRM task creation
    ai_port = OpenRouterAdapter(
        api_key=settings.openrouter_api_key or settings.openai_api_key or ""
    )
    create_twenty_task = CreateTwentyTaskFromSession(twenty_adapter, ai_port=ai_port)

    # Dispatcher with Redis FSM storage (shared state across per-request instances)
    storage = RedisStorage(redis=redis)
    dp = Dispatcher(storage=storage)

    @dp.error()
    async def on_telegram_error(event: ErrorEvent) -> bool:
        if isinstance(event.exception, TelegramAPIError):
            logger.warning("Telegram API error (non-fatal): %s", event.exception)
            return True  # suppress — don't propagate to FastAPI
        return False  # re-raise non-Telegram errors

    dp.include_router(
        create_router(
            start_session,
            add_text,
            add_voice,
            trigger_analysis,
            cancel_session,
            user_port,
            auto_register=auto_register,
            ai_port=ai_port,
            set_analysis_result=set_analysis_result,
            create_twenty_task=create_twenty_task,
            draft_repo=draft_repo,
            twenty_crm_port=twenty_adapter,
            redis=redis,
            bot_username=settings.telegram_bot_username,
        )
    )
    dp.include_router(
        create_tasks_router(
            get_my_tasks, update_task_status, reassign_task, add_task_comment, user_port
        )
    )
    dp.include_router(create_settings_router(update_profile, save_voice, user_port))
    dp.include_router(create_call_notification_router(call_repo))

    update = Update.model_validate(update_data)
    await dp.feed_update(bot, update)

    return {"ok": "true"}
