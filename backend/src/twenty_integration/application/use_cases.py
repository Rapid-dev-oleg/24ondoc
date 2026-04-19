"""Twenty Integration — Application Use Cases."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from ai_classification.domain.repository import AIClassificationPort
from telegram_ingestion.domain.models import DraftSession
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

        task = await self._port.create_task(
            title=session.ai_result.title,
            body=session.ai_result.description,
            due_at=_parse_deadline(session.ai_result.deadline),
            assignee_id=assignee_id,
            kategoriya=kategoriya,
            vazhnost=vazhnost,
            klient_id=klient_id,
            location_rel_id=location_rel_id,
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
        """Найти или создать Person + Location по номеру телефона.

        Обогащает пустые location-поля на Person из AI-извлечения. Не трогает
        уже заполненные поля. Возвращает (klient_id, location_rel_id), любые
        могут быть None если phone отсутствует или AI / Twenty недоступны.
        """
        if not caller_phone:
            return None, None

        try:
            person = await self._port.find_person_by_phone(caller_phone)
            if person is None:
                person = await self._port.create_person_with_phone(caller_phone)
            klient_id = person.get("id") or None

            # Location lookup/creation — independent of Person
            location = await self._port.find_location_by_phone(caller_phone)

            # Extract location fields from dialogue once, if we have it
            extracted = {"prefix": None, "number": None, "address": None}
            if dialogue_text and self._ai_port is not None:
                extract_fn = getattr(self._ai_port, "extract_location", None)
                if extract_fn is not None:
                    try:
                        extracted = await extract_fn(dialogue_text)
                    except Exception:
                        logger.exception("extract_location failed")

            if location is None:
                location = await self._port.create_location(
                    caller_phone,
                    prefix=extracted["prefix"],
                    number=extracted["number"],
                    address=extracted["address"],
                )
            else:
                # Fill empty fields only — don't overwrite admin-edited data
                patch = {
                    k: v
                    for k, v in [
                        ("prefix", extracted["prefix"]),
                        ("number", extracted["number"]),
                        ("address", extracted["address"]),
                    ]
                    if v and not location.get(
                        {"prefix": "prefix", "number": "number", "address": "locationAddress"}[k]
                    )
                }
                if patch:
                    await self._port.update_location(location["id"], **patch)

            location_rel_id = location.get("id") or None

            if klient_id and location_rel_id:
                try:
                    await self._port.link_person_to_location(klient_id, location_rel_id)
                except Exception:
                    logger.exception("link_person_to_location failed")

            # Also refresh Person's cached location-* fields (empty ones only)
            if klient_id:
                try:
                    to_fill: dict[str, str | None] = {}
                    if extracted["prefix"] and not person.get("locationPrefix"):
                        to_fill["location_prefix"] = extracted["prefix"]
                    if extracted["number"] and not person.get("locationNumber"):
                        to_fill["location_number"] = extracted["number"]
                    if extracted["address"] and not person.get("locationAddress"):
                        to_fill["location_address"] = extracted["address"]
                    if to_fill:
                        await self._port.update_person_location_fields(klient_id, **to_fill)
                except Exception:
                    logger.exception("update_person_location_fields failed")

            return klient_id, location_rel_id
        except Exception:
            logger.exception("_resolve_person_and_location failed for phone=%s", caller_phone)
            return None, None
