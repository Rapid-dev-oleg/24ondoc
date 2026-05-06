"""Twenty Integration — Application Use Cases."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from ai_classification.domain.repository import AIClassificationPort
from telegram_ingestion.domain.models import DraftSession
from twenty_integration.application.detect_repeat import DetectRepeat, RepeatResult
from twenty_integration.domain.models import TwentyTask
from twenty_integration.domain.ports import TwentyCRMPort

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    FileDownloader = Callable[[str], Coroutine[None, None, tuple[bytes, str, str] | None]]

logger = logging.getLogger(__name__)


def _parse_deadline(deadline_str: str | None) -> datetime | None:
    """Парсить строку дедлайна в datetime. Возвращает None если парсинг невозможен."""
    if deadline_str is None:
        return None
    try:
        return datetime.fromisoformat(deadline_str)
    except (ValueError, TypeError):
        return None


class CreateTwentyTaskFromSession:
    """Use Case: создать задачу в Twenty из завершённой DraftSession."""

    def __init__(self, port: TwentyCRMPort, ai_port: AIClassificationPort | None = None) -> None:
        self._port = port
        self._ai_port = ai_port
        # Repeat detector composed here so we don't rewire callers; relies on
        # the same TwentyCRMPort + AI adapter. No-op when location cannot be
        # resolved.
        self._detect_repeat = DetectRepeat(twenty_port=port, ai_port=ai_port)

    async def execute(
        self,
        session: DraftSession,
        telegram_id: int,
        user_name: str,
        assignee_id: str | None = None,
        file_downloader: FileDownloader | None = None,
        kategoriya: str | None = None,
        vazhnost: str | None = None,
        *,
        caller_phone: str | None = None,
        dialogue_text: str | None = None,
    ) -> TwentyTask:
        """Создать задачу в Twenty из сессии.

        Args:
            session: Завершённая DraftSession со статусом PREVIEW
            telegram_id: Telegram ID пользователя
            user_name: Имя пользователя
            assignee_id: ID ответственного (опционально)
            file_downloader: Async callback (file_id) -> (bytes, filename) | None
            kategoriya: Pre-selected kategoriya value from Twenty options
            vazhnost: Pre-selected vazhnost value from Twenty options

        Returns:
            Созданная TwentyTask
        """
        if session.ai_result is None:
            raise ValueError("DraftSession должна иметь ai_result")

        # If kategoriya/vazhnost not pre-selected, try to determine now
        if kategoriya is None and vazhnost is None and self._ai_port is not None:
            try:
                options = await self._port.fetch_task_field_options()
                task_text = f"{session.ai_result.title}\n{session.ai_result.description}"
                selection = await self._ai_port.select_task_fields(
                    task_text,
                    options.get("kategoriya", []),
                    options.get("vazhnost", []),
                )
                kategoriya = selection.kategoriya
                vazhnost = selection.vazhnost
            except Exception:
                logger.exception("Failed to select task fields, creating without them")

        # If a caller phone is known, resolve Person and Location in Twenty
        # so the task is anchored to the right client and outlet.
        klient_id, location_rel_id = await self._resolve_person_and_location(
            caller_phone, dialogue_text
        )

        # Detect repeat obrashchenie BEFORE we create the task — so the
        # Task is born with the correct povtornoeObrashchenie and
        # parentTaskId, without an extra PATCH round-trip.
        repeat = RepeatResult(False, None, "none", 0)
        try:
            repeat = await self._detect_repeat.execute(
                location_id=str(location_rel_id) if location_rel_id else None,
                new_dialogue=dialogue_text or "",
            )
        except Exception:
            logger.exception("DetectRepeat failed; proceeding with is_repeat=False")

        task = await self._port.create_task(
            title=session.ai_result.title,
            body=session.ai_result.description,
            due_at=_parse_deadline(session.ai_result.deadline),
            assignee_id=assignee_id,
            kategoriya=kategoriya,
            vazhnost=vazhnost,
            klient_id=klient_id,
            location_rel_id=location_rel_id,
            povtornoe_obrashchenie=repeat.is_repeat,
            parent_task_id=repeat.parent_task_id,
        )

        # 4. Загрузить файлы в Twenty и прикрепить к задаче
        if file_downloader is not None:
            for block in session.content_blocks:
                if block.type in ("photo", "file") and block.file_id:
                    try:
                        result = await file_downloader(block.file_id)
                        if result is not None:
                            file_bytes, filename, content_type = result
                            # Upload file to Twenty storage
                            path = await self._port.upload_file(file_bytes, filename, content_type)
                            if path:
                                await self._port.create_attachment(task.twenty_id, filename, path)
                    except Exception:
                        logger.exception(
                            "Failed to attach file %s to task %s",
                            block.file_id,
                            task.twenty_id,
                        )

        return task

    async def _resolve_person_and_location(
        self,
        caller_phone: str | None,
        dialogue_text: str | None,
    ) -> tuple[str | None, str | None]:
        """Resolve Person and Location for an incoming call.

        Точки заводятся каталогом из xlsx и НИКОГДА не создаются автоматически —
        только привязываются. Резолв точки:
            1. AI-extract имени точки из транскрипта (matches displayName)
            2. fallback: поиск по телефону. 0 или >1 результатов → None+WARN.

        Person по-прежнему создаётся автоматически (контактов не блокируем).
        """
        if not caller_phone:
            return None, None

        klient_id: str | None = None
        try:
            person = await self._port.find_person_by_phone(caller_phone)
            if person is None:
                person = await self._port.create_person_with_phone(caller_phone)
            klient_id = person.get("id") or None
        except Exception:
            logger.exception(
                "_resolve_person_and_location: person resolution failed phone=%s",
                caller_phone,
            )

        location_rel_id = await self._resolve_location(caller_phone, dialogue_text)
        return klient_id, location_rel_id

    async def _resolve_location(
        self,
        caller_phone: str | None,
        dialogue_text: str | None,
    ) -> str | None:
        # Step 1: AI extract location name from transcript.
        if dialogue_text and self._ai_port is not None:
            extract_fn = getattr(self._ai_port, "extract_location_name", None)
            if extract_fn is not None:
                try:
                    known = await self._port.list_location_display_names()
                    name = await extract_fn(dialogue_text, known)
                    if name:
                        loc = await self._port.find_location_by_display_name(name)
                        if loc and loc.get("id"):
                            return str(loc["id"])
                        logger.warning(
                            "AI extracted location name %r not found in catalog", name
                        )
                except Exception:
                    logger.exception("extract_location_name failed")

        # Step 2: phone fallback. Ambiguous (>1) means a roving manager — leave
        # the task without a location and let an operator pick.
        if caller_phone:
            try:
                candidates = await self._port.find_locations_by_phone(caller_phone)
            except Exception:
                logger.exception(
                    "find_locations_by_phone failed phone=%s", caller_phone
                )
                candidates = []
            if len(candidates) == 1:
                return str(candidates[0].get("id") or "") or None
            if len(candidates) > 1:
                names = [str(c.get("displayName") or c.get("id")) for c in candidates]
                logger.warning(
                    "ambiguous location for phone=%s: %d candidates %s",
                    caller_phone,
                    len(candidates),
                    names,
                )
            else:
                logger.info("no location matched phone=%s", caller_phone)
        return None
