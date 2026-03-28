"""Admin panel — Telegram notification adapter (Bot API sendMessage)."""

from __future__ import annotations

import logging

import httpx

from admin.application.ports import TelegramNotificationPort

logger = logging.getLogger(__name__)


class TelegramNotifyAdapter(TelegramNotificationPort):
    """Sends messages via Telegram Bot API."""

    def __init__(self, bot_token: str) -> None:
        self._bot_token = bot_token
        self._http = httpx.AsyncClient(timeout=10.0)

    async def send_message(self, telegram_id: int, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {"chat_id": telegram_id, "text": text}
        try:
            response = await self._http.post(url, json=payload)
            if response.status_code >= 400:
                logger.warning(
                    "Telegram sendMessage failed %s: %s", response.status_code, response.text
                )
        except Exception as exc:
            logger.warning("Telegram sendMessage error: %s", exc)
