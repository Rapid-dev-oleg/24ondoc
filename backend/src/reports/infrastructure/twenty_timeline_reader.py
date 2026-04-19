"""Twenty timelineActivity reader — minimal fetcher for the reports module.

All we need for the per-operator report is:
  - task.updated events (for status/assignee diffs),
  - the current snapshot of tasks (vazhnost, status, assigneeId, povtornoeObrashchenie, scriptViolations),
  - workspaceMembers (for name resolution).

The reader does the HTTP + pagination; the pure `compute_report` function
takes these plain lists and produces the ReportDTO. Keeping them separate
makes compute_report fully unit-testable without mocking HTTP.

Event volume on this project is small (~600 total so far). We fetch ALL
task.updated events (not just in-window) so that, for tasks closed in the
window but whose last assignment happened earlier, we can still find
`received_at`. If the dataset outgrows memory this can be optimized with
a window + per-task lookup fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class TimelineData:
    updated_events: tuple[dict[str, Any], ...]
    tasks: tuple[dict[str, Any], ...]
    members_by_id: dict[str, str]  # wmid -> display name
    # task.created — real Twenty INSERT ts. Optional for test ergonomics;
    # production loader always populates.
    created_events: tuple[dict[str, Any], ...] = ()


class TwentyTimelineReader:
    """Reads the three data sources the report needs from Twenty REST."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> TwentyTimelineReader:
        return self

    async def __aexit__(self, *_a: object) -> None:
        await self.close()

    async def _page_all(
        self, path: str, plural: str, base_filter: str = "",
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(100):
            params: dict[str, Any] = {
                "limit": 100,
                "order_by": "createdAt[AscNullsFirst]",
            }
            if base_filter and cursor:
                params["filter"] = f"{base_filter},createdAt[gt]:{cursor}"
            elif base_filter:
                params["filter"] = base_filter
            elif cursor:
                params["filter"] = f"createdAt[gt]:{cursor}"
            r = await self._client.get(path, params=params)
            r.raise_for_status()
            items = r.json().get("data", {}).get(plural, [])
            if not items:
                break
            out.extend(items)
            cursor = items[-1].get("createdAt")
            if len(items) < 100:
                break
        return out

    async def load(self) -> TimelineData:
        updated = await self._page_all(
            "/rest/timelineActivities", "timelineActivities",
            "name[eq]:task.updated",
        )
        created = await self._page_all(
            "/rest/timelineActivities", "timelineActivities",
            "name[eq]:task.created",
        )
        tasks = await self._page_all("/rest/tasks", "tasks")
        members_raw = await self._page_all(
            "/rest/workspaceMembers", "workspaceMembers",
        )
        members_by_id: dict[str, str] = {}
        for m in members_raw:
            wmid = m.get("id")
            if not wmid:
                continue
            name = m.get("name") or {}
            fn = (name.get("firstName") or "").strip()
            ln = (name.get("lastName") or "").strip()
            members_by_id[wmid] = (f"{fn} {ln}".strip()) or wmid[:8]
        return TimelineData(
            updated_events=tuple(updated),
            created_events=tuple(created),
            tasks=tuple(tasks),
            members_by_id=members_by_id,
        )
