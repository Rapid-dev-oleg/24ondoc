"""Twenty Integration — Application Use Cases."""

from __future__ import annotations

from datetime import datetime

from telegram_ingestion.domain.models import DraftSession
from twenty_integration.domain.models import TwentyTask
from twenty_integration.domain.ports import TwentyCRMPort


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

    def __init__(self, port: TwentyCRMPort) -> None:
        self._port = port

    async def execute(
        self,
        session: DraftSession,
        telegram_id: int,
        user_name: str,
        assignee_id: str | None = None,
    ) -> TwentyTask:
        """Создать задачу в Twenty из сессии.

        Args:
            session: Завершённая DraftSession со статусом PREVIEW
            telegram_id: Telegram ID пользователя
            user_name: Имя пользователя
            assignee_id: ID ответственного (опционально)

        Returns:
            Созданная TwentyTask
        """
        if session.ai_result is None:
            raise ValueError("DraftSession должна иметь ai_result")

        # 1. Найти или создать Person по telegram_id
        person = await self._port.find_person_by_telegram_id(telegram_id)
        if person is None:
            person = await self._port.create_person(telegram_id, user_name)

        # 2. Создать Task
        task = await self._port.create_task(
            title=session.ai_result.title,
            body=session.ai_result.description,
            due_at=_parse_deadline(session.ai_result.deadline),
            assignee_id=assignee_id,
        )

        # 3. Связать Person с Task
        await self._port.link_person_to_task(task.twenty_id, person.twenty_id)

        return task
