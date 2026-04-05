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
    ) -> TwentyTask:
        """Создать задачу в Twenty из сессии.

        Args:
            session: Завершённая DraftSession со статусом PREVIEW
            telegram_id: Telegram ID пользователя
            user_name: Имя пользователя
            assignee_id: ID ответственного (опционально)
            file_downloader: Async callback (file_id) -> (bytes, filename) | None

        Returns:
            Созданная TwentyTask
        """
        if session.ai_result is None:
            raise ValueError("DraftSession должна иметь ai_result")

        # 1. Подобрать kategoriya и vazhnost из актуальных списков Twenty
        kategoriya_value: str | None = None
        vazhnost_value: str | None = None
        if self._ai_port is not None:
            try:
                options = await self._port.fetch_task_field_options()
                task_text = f"{session.ai_result.title}\n{session.ai_result.description}"
                selection = await self._ai_port.select_task_fields(
                    task_text,
                    options.get("kategoriya", []),
                    options.get("vazhnost", []),
                )
                kategoriya_value = selection.kategoriya
                vazhnost_value = selection.vazhnost
            except Exception:
                logger.exception("Failed to select task fields, creating without them")

        # 2. Создать Task с назначением оператора как ответственного (workspace member)
        task = await self._port.create_task(
            title=session.ai_result.title,
            body=session.ai_result.description,
            due_at=_parse_deadline(session.ai_result.deadline),
            assignee_id=assignee_id,
            kategoriya=kategoriya_value,
            vazhnost=vazhnost_value,
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
                            path = await self._port.upload_file(
                                file_bytes, filename, content_type
                            )
                            if path:
                                await self._port.create_attachment(
                                    task.twenty_id, filename, path
                                )
                    except Exception:
                        logger.exception(
                            "Failed to attach file %s to task %s",
                            block.file_id,
                            task.twenty_id,
                        )

        return task
