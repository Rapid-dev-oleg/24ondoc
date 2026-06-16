"""Single-purpose location resolver, shared by Telegram and ATS paths.

Pulled out of CreateTwentyTaskFromSession so the ATS poller can reuse the
same three-stage logic without copy-pasting it. The poller and the
Telegram use-case both compose this class with the same TwentyCRMPort
and AIClassificationPort.
"""
from __future__ import annotations

import logging

from ai_classification.domain.repository import AIClassificationPort
from twenty_integration.application.location_match import (
    match_displayname_in_text,
)
from twenty_integration.domain.ports import TwentyCRMPort

logger = logging.getLogger(__name__)


class ResolveLocation:
    """Resolve a Twenty Location for an incoming call/message.

    Stages, applied in order:
      1) cheap fold-match against catalog displayName
         (latin↔cyrillic homoglyphs, leading-zero strip,
         word-boundary substring search; see `location_match`),
      2) AI extract via `extract_location_name` (Whisper-tolerant prompt),
      3) phone fallback against Location.phone (primary + additional);
         skipped when >1 location matches the same phone.

    Successful name- and AI-based resolves trigger learn-by-resolve:
    the caller's phone is appended to Location.additionalPhones if not
    already there, so the next call from this number resolves cheaply
    via stage 3 without paying for an AI round-trip.
    """

    def __init__(
        self,
        twenty_port: TwentyCRMPort,
        ai_port: AIClassificationPort | None = None,
    ) -> None:
        self._port = twenty_port
        self._ai_port = ai_port

    async def execute(
        self,
        caller_phone: str | None,
        dialogue_text: str | None,
    ) -> str | None:
        catalog: list[str] | None = None

        # Stage 1 — cheap fold-match.
        if dialogue_text:
            try:
                catalog = await self._port.list_location_display_names()
                cheap = match_displayname_in_text(
                    dialogue_text,
                    [(n, n) for n in catalog],
                )
                if cheap:
                    loc = await self._port.find_location_by_display_name(cheap)
                    if loc and loc.get("id"):
                        loc_id = str(loc["id"])
                        await self._learn_phone(loc_id, caller_phone)
                        return loc_id
            except Exception:
                logger.exception("cheap fold-match failed")

        # Stage 2 — AI.
        if dialogue_text and self._ai_port is not None:
            extract_fn = getattr(self._ai_port, "extract_location_name", None)
            if extract_fn is not None:
                try:
                    if catalog is None:
                        catalog = await self._port.list_location_display_names()
                    name = await extract_fn(dialogue_text, catalog)
                    if name:
                        loc = await self._port.find_location_by_display_name(name)
                        if loc and loc.get("id"):
                            loc_id = str(loc["id"])
                            await self._learn_phone(loc_id, caller_phone)
                            return loc_id
                        logger.warning(
                            "AI extracted location name %r not found in catalog",
                            name,
                        )
                except Exception:
                    logger.exception("extract_location_name failed")

        # Stage 3 — phone fallback. Ambiguous (>1) means a roving manager —
        # leave Task without a location and let an operator pick.
        if caller_phone:
            try:
                candidates = await self._port.find_locations_by_phone(caller_phone)
            except Exception:
                logger.exception(
                    "find_locations_by_phone failed phone=%s", caller_phone,
                )
                candidates = []
            if len(candidates) == 1:
                return str(candidates[0].get("id") or "") or None
            if len(candidates) > 1:
                names = [
                    str(c.get("displayName") or c.get("id"))
                    for c in candidates
                ]
                logger.warning(
                    "ambiguous location for phone=%s: %d candidates %s",
                    caller_phone, len(candidates), names,
                )
            else:
                logger.info("no location matched phone=%s", caller_phone)
        return None

    async def _learn_phone(
        self, location_id: str, caller_phone: str | None,
    ) -> None:
        """Best-effort: attach caller phone to the resolved Location."""
        if not caller_phone or not location_id:
            return
        try:
            await self._port.add_phone_to_location(location_id, caller_phone)
        except Exception:
            logger.exception(
                "learn-by-resolve failed loc=%s phone=%s",
                location_id, caller_phone,
            )
