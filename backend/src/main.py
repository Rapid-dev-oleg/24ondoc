"""FastAPI application entry point."""
from __future__ import annotations

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = structlog.get_logger(__name__)

app = FastAPI(
    title="24ondoc Backend",
    description="Chatwoot CRM + Telegram Bot + АТС Т2 integration",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> dict[str, str]:
    return {"status": "ok"}


# Routers будут подключены здесь по мере реализации:
# from src.ats_processing.infrastructure.webhook_handler import router as t2_router
# from src.telegram_ingestion.infrastructure.bot_handler import router as tg_router
# from src.chatwoot_integration.infrastructure.chatwoot_client import router as cw_router
# app.include_router(t2_router, prefix="/webhook/t2")
# app.include_router(tg_router, prefix="/webhook/telegram")
# app.include_router(cw_router, prefix="/webhook/chatwoot")
