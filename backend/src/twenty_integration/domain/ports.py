"""Twenty Integration — Abstract Port."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from .models import TwentyMember, TwentyPerson, TwentyTask


class TwentyCRMPort(ABC):
    """Anti-Corruption Layer: интерфейс к Twenty CRM REST API."""

    @abstractmethod
    async def list_workspace_members(self) -> list[TwentyMember]: ...

    @abstractmethod
    async def find_person_by_telegram_id(self, telegram_id: int) -> TwentyPerson | None: ...

    @abstractmethod
    async def create_person(self, telegram_id: int, name: str) -> TwentyPerson: ...

    @abstractmethod
    async def fetch_task_field_options(self) -> dict[str, list[dict[str, str]]]:
        """Fetch current kategoriya and vazhnost options from Twenty metadata.

        Returns dict like:
            {"kategoriya": [{"label": "...", "value": "..."}, ...],
             "vazhnost": [{"label": "...", "value": "..."}, ...]}
        """
        ...

    @abstractmethod
    async def create_task(
        self,
        title: str,
        body: str,
        due_at: datetime | None,
        assignee_id: str | None,
        kategoriya: str | None = None,
        vazhnost: str | None = None,
        *,
        klient_id: str | None = None,
        location_rel_id: str | None = None,
        call_record_rel_id: str | None = None,
        povtornoe_obrashchenie: bool | None = None,
        parent_task_id: str | None = None,
    ) -> TwentyTask: ...

    @abstractmethod
    async def find_person_by_phone(self, phone: str) -> dict[str, object] | None: ...

    @abstractmethod
    async def create_person_with_phone(
        self, phone: str, name: str | None = None
    ) -> dict[str, object]: ...

    @abstractmethod
    async def update_person_location_fields(
        self,
        person_id: str,
        *,
        location_prefix: str | None = None,
        location_number: str | None = None,
        location_address: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def find_location_by_phone(self, phone: str) -> dict[str, object] | None: ...

    @abstractmethod
    async def create_location(
        self,
        phone: str,
        *,
        prefix: str | None = None,
        number: str | None = None,
        address: str | None = None,
    ) -> dict[str, object]: ...

    @abstractmethod
    async def update_location(
        self,
        location_id: str,
        *,
        prefix: str | None = None,
        number: str | None = None,
        address: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def link_person_to_location(self, person_id: str, location_id: str) -> None: ...

    @abstractmethod
    async def find_recent_tasks_by_location_id(
        self, location_id: str, since: datetime, limit: int = 10
    ) -> list[dict[str, object]]: ...

    @abstractmethod
    async def find_call_record_by_ats_id(self, ats_call_id: str) -> dict[str, object] | None: ...

    @abstractmethod
    async def create_call_record(
        self,
        ats_call_id: str,
        *,
        caller_phone: str | None = None,
        direction: str = "INCOMING",
        duration: int | None = None,
        call_status: str = "ANSWERED",
        occurred_at: datetime | None = None,
        transcript: str | None = None,
        audio_url: str | None = None,
        person_rel_id: str | None = None,
        location_rel_id: str | None = None,
        task_rel_id: str | None = None,
    ) -> dict[str, object]: ...

    @abstractmethod
    async def update_call_record(
        self,
        call_record_id: str,
        *,
        task_rel_id: str | None = None,
        person_rel_id: str | None = None,
        location_rel_id: str | None = None,
        transcript: str | None = None,
    ) -> None: ...

    @abstractmethod
    async def link_person_to_task(self, task_id: str, person_id: str) -> None: ...

    @abstractmethod
    async def upload_file(
        self, file_bytes: bytes, filename: str, content_type: str
    ) -> str | None: ...

    @abstractmethod
    async def create_attachment(self, task_id: str, name: str, file_path: str) -> None: ...

    @abstractmethod
    async def update_task_body(self, task_id: str, body: str) -> None: ...

    @abstractmethod
    async def get_task(self, task_id: str) -> dict[str, object] | None: ...

    @abstractmethod
    async def update_task_script_check(
        self, task_id: str, violations: int, missing: list[str]
    ) -> None: ...
