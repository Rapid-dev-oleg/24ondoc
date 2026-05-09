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
        location_rel_id: str | None = None,
        caller_phone: str | None = None,
        call_record_rel_id: str | None = None,
        povtornoe_obrashchenie: bool | None = None,
        parent_task_id: str | None = None,
    ) -> TwentyTask: ...

    @abstractmethod
    async def find_locations_by_phone(self, phone: str) -> list[dict[str, object]]:
        """Все Точки, у которых данный номер совпадает с primary или additional.

        Возвращает список (может быть пуст или содержать несколько точек —
        один номер легально привязан к нескольким точкам, например выездной
        менеджер). Никогда не создаёт.
        """
        ...

    @abstractmethod
    async def find_location_by_display_name(
        self, display_name: str
    ) -> dict[str, object] | None: ...

    @abstractmethod
    async def list_location_display_names(self) -> list[str]:
        """Каталог имён точек для AI-extract. Возвращает все displayName."""
        ...

    @abstractmethod
    async def add_phone_to_location(
        self, location_id: str, phone: str
    ) -> bool:
        """Learn-by-resolve: дописать `phone` в Location.phone, если его \
там ещё нет. Если primary пустой — кладём в primary; иначе — в \
additionalPhones. Возвращает True если запись была сделана, False если \
номер уже был у точки или phone не нормализуется.
        """
        ...

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
        callee_phone: str | None = None,
        direction: str = "INCOMING",
        duration: int | None = None,
        call_status: str = "ANSWERED",
        occurred_at: datetime | None = None,
        transcript: str | None = None,
        audio_url: str | None = None,
        location_rel_id: str | None = None,
        task_rel_id: str | None = None,
    ) -> dict[str, object]: ...

    @abstractmethod
    async def update_call_record(
        self,
        call_record_id: str,
        *,
        task_rel_id: str | None = None,
        location_rel_id: str | None = None,
        transcript: str | None = None,
        callee_phone: str | None = None,
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
