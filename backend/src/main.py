"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from admin.infrastructure.router import router as admin_router
from ats_processing.application.ats2_poller import ATS2PollerService
from ats_processing.application.ats2_transcription_mapper import ATS2TranscriptionMapper
from ats_processing.infrastructure.ats2_client import ATS2AuthManager, ATS2RestClient
from ats_processing.infrastructure.repository import CallRecordRepositoryImpl
from ats_processing.infrastructure.webhook_handler import router as t2_router
from config import Settings, get_settings
from telegram_ingestion.infrastructure.stt_adapter import OpenRouterSTTAdapter
from telegram_ingestion.infrastructure.telegram_fastapi import router as tg_router
from twenty_integration.infrastructure.twenty_adapter import TwentyRestAdapter

logger = structlog.get_logger(__name__)


from ai_classification.infrastructure.openrouter_adapter import OpenRouterAdapter


class _PollerCallRepo:
    """CallRecordRepository wrapper for ATS2 poller (uses its own session per operation)."""

    def __init__(self, session_factory: async_sessionmaker) -> None:  # type: ignore[type-arg]
        self._session_factory = session_factory

    async def get_by_id(self, call_id: str) -> object | None:
        async with self._session_factory() as session:
            async with session.begin():
                repo = CallRecordRepositoryImpl(session)
                return await repo.get_by_id(call_id)

    async def save(self, record: object) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                repo = CallRecordRepositoryImpl(session)
                await repo.save(record)  # type: ignore[arg-type]

    async def get_pending(self, limit: int = 10, source: object = None) -> list:  # type: ignore[type-arg]
        return []

    async def get_recent(self, limit: int = 10) -> list:  # type: ignore[type-arg]
        async with self._session_factory() as session:
            async with session.begin():
                repo = CallRecordRepositoryImpl(session)
                return await repo.get_recent(limit)

    async def find_recent_by_phone(self, phone: str, limit: int = 10) -> list:  # type: ignore[type-arg]
        return []


def _create_ats2_poller(
    settings: Settings,
    session_factory: async_sessionmaker,  # type: ignore[type-arg]
    twenty_adapter: TwentyRestAdapter | None = None,
    redis: AsyncRedis | None = None,
) -> ATS2PollerService | None:
    """Create ATS2PollerService if ATS2_ENABLED=true, else return None."""
    if not settings.ats2_enabled:
        return None

    auth_manager = ATS2AuthManager(
        access_token=settings.ats2_access_token,
        refresh_token=settings.ats2_refresh_token,
        base_url=settings.ats2_base_url,
        proxy_url=settings.ats2_proxy_url,
    )
    ats2_client = ATS2RestClient(
        auth_manager=auth_manager,
        base_url=settings.ats2_base_url,
        proxy_url=settings.ats2_proxy_url,
    )
    mapper = ATS2TranscriptionMapper()
    call_repo = _PollerCallRepo(session_factory)

    ai_port = OpenRouterAdapter(
        api_key=settings.openrouter_api_key or settings.openai_api_key or ""
    )

    stt_port = OpenRouterSTTAdapter(
        api_key=settings.openai_api_key or settings.openrouter_api_key,
        whisper_url=settings.whisper_base_url,
    )

    return ATS2PollerService(
        ats2_client=ats2_client,
        call_repo=call_repo,  # type: ignore[arg-type]
        transcription_mapper=mapper,
        ai_port=ai_port,
        twenty_port=twenty_adapter if settings.twenty_api_key else None,
        stt_port=stt_port,
        redis=redis,
        poll_interval_sec=float(settings.ats2_poll_interval_sec),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()

    # SQLAlchemy async engine
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Redis
    redis: AsyncRedis = AsyncRedis.from_url(settings.redis_url, decode_responses=False)

    # Telegram Bot
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )

    # Twenty CRM adapter
    if not settings.twenty_api_key:
        logger.warning("TWENTY_API_KEY is empty — Twenty CRM integration disabled")
    twenty_adapter = TwentyRestAdapter(
        base_url=settings.twenty_base_url,
        api_key=settings.twenty_api_key,
    )

    # STT adapter: Groq primary, self-hosted Whisper fallback, OpenAI last
    stt_port = OpenRouterSTTAdapter(
        api_key=settings.openai_api_key or settings.openrouter_api_key,
        whisper_url=settings.whisper_base_url,
        groq_api_key=settings.groq_api_key,
    )

    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.state.bot = bot
    app.state.twenty_adapter = twenty_adapter
    app.state.stt_port = stt_port
    app.state.settings = settings

    # Register Telegram webhook
    webhook_url = f"{settings.telegram_webhook_base_url}/webhook/telegram"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=settings.telegram_webhook_secret,
        drop_pending_updates=True,
    )
    logger.info("Telegram webhook registered", url=webhook_url)

    # ATS2 Poller (background task)
    ats2_poller = _create_ats2_poller(
        settings,
        session_factory=session_factory,
        twenty_adapter=twenty_adapter if settings.twenty_api_key else None,
        redis=redis,
    )
    ats2_task: asyncio.Task[None] | None = None
    if ats2_poller is not None:
        ats2_task = asyncio.create_task(ats2_poller.start())
        app.state.ats2_poller = ats2_poller
        logger.info("ATS2 Poller started", interval=settings.ats2_poll_interval_sec)
    else:
        logger.info("ATS2 Poller disabled (ATS2_ENABLED=false)")

    logger.info("Application started")
    yield

    # Shutdown ATS2 Poller
    if ats2_poller is not None:
        ats2_poller.stop()
        if ats2_task is not None:
            await asyncio.wait_for(ats2_task, timeout=10.0)
        logger.info("ATS2 Poller stopped")

    await bot.session.close()
    await redis.aclose()
    await engine.dispose()
    logger.info("Application stopped")


app = FastAPI(
    title="24ondoc Backend",
    description="Twenty CRM + Telegram Bot + АТС Т2 integration",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return JSON 500 for all unhandled exceptions instead of Starlette's plain-text response."""
    logger.error("Unhandled exception", path=request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.middleware("http")
async def db_session_middleware(request: Request, call_next: object) -> Response:
    """Create a per-request SQLAlchemy session and inject repos into request.state."""
    from collections.abc import Awaitable, Callable

    _call_next = cast(Callable[[Request], Awaitable[Response]], call_next)
    # Skip lifespan-internal requests that don't need DB
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        return await _call_next(request)

    async with session_factory() as session:
        async with session.begin():
            request.state.db_session = session
            request.state.call_repo = CallRecordRepositoryImpl(session)
            request.state.t2_webhook_secret = request.app.state.settings.t2_webhook_secret
            response: Response = await _call_next(request)
    return response


import os

from fastapi.responses import FileResponse

_ATTACHMENTS_DIR = "/app/data/attachments"


@app.get("/api/files/{filename:path}")
async def serve_attachment(filename: str) -> FileResponse:
    """Serve uploaded attachment files."""
    filepath = os.path.join(_ATTACHMENTS_DIR, filename)
    if not os.path.isfile(filepath):
        return JSONResponse(status_code=404, content={"detail": "File not found"})  # type: ignore[return-value]
    return FileResponse(filepath)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> dict[str, str]:
    return {"status": "ok"}


# Handlers already define full paths — include WITHOUT extra prefix
app.include_router(admin_router)  # /api/admin/*
app.include_router(t2_router)  # POST /webhook/t2/call
app.include_router(tg_router)  # POST /webhook/telegram
