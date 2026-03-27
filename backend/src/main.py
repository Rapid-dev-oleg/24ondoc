"""FastAPI application entry point."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from admin.infrastructure.router import router as admin_router
from ats_processing.infrastructure.repository import CallRecordRepositoryImpl
from ats_processing.infrastructure.webhook_handler import router as t2_router
from chatwoot_integration.infrastructure.chatwoot_client import ChatwootClient
from chatwoot_integration.infrastructure.ticket_repository import (
    InMemorySupportTicketRepository,
)
from chatwoot_integration.infrastructure.webhook_handler import router as cw_router
from config import get_settings
from telegram_ingestion.infrastructure.stt_adapter import OpenRouterSTTAdapter
from telegram_ingestion.infrastructure.telegram_fastapi import router as tg_router

logger = structlog.get_logger(__name__)


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

    # Chatwoot client (singleton — uses httpx, no per-request state)
    chatwoot_client = ChatwootClient(
        base_url=settings.chatwoot_base_url,
        api_key=settings.chatwoot_api_key,
        account_id=settings.chatwoot_account_id,
        redis=redis,
    )

    # STT adapter (bytes → text via OpenAI-compatible API / OpenRouter)
    stt_port = OpenRouterSTTAdapter(api_key=settings.openai_api_key or settings.openrouter_api_key)

    # In-memory SupportTicket cache (persists for process lifetime)
    ticket_repo = InMemorySupportTicketRepository()

    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.state.bot = bot
    app.state.chatwoot_client = chatwoot_client
    app.state.stt_port = stt_port
    app.state.ticket_repo = ticket_repo
    app.state.settings = settings

    # Register Telegram webhook
    webhook_url = f"{settings.telegram_webhook_base_url}/webhook/telegram"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=settings.telegram_webhook_secret,
        drop_pending_updates=True,
    )
    logger.info("Telegram webhook registered", url=webhook_url)

    logger.info("Application started")
    yield

    await bot.session.close()
    await redis.aclose()
    await engine.dispose()
    logger.info("Application stopped")


app = FastAPI(
    title="24ondoc Backend",
    description="Chatwoot CRM + Telegram Bot + АТС Т2 integration",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def db_session_middleware(request: Request, call_next: object) -> Response:
    """Create a per-request SQLAlchemy session and inject repos into request.state."""
    # Skip lifespan-internal requests that don't need DB
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        from collections.abc import Callable
        return await (call_next)(request)  # type: ignore[arg-type]

    from collections.abc import Callable

    async with session_factory() as session:
        async with session.begin():
            request.state.db_session = session
            request.state.call_repo = CallRecordRepositoryImpl(session)
            request.state.ticket_repo = request.app.state.ticket_repo
            request.state.t2_webhook_secret = request.app.state.settings.t2_webhook_secret
            response: Response = await (call_next)(request)  # type: ignore[arg-type]
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> dict[str, str]:
    return {"status": "ok"}


# Handlers already define full paths — include WITHOUT extra prefix
app.include_router(admin_router)  # /api/admin/*
app.include_router(t2_router)     # POST /webhook/t2/call
app.include_router(cw_router)     # POST /webhook/chatwoot
app.include_router(tg_router)     # POST /webhook/telegram
