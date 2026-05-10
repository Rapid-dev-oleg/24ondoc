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

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Twenty REST permits ~100 ops/60s shared across reads+writes. We page
# through up to 4 endpoints × N pages; bursting that is what's been
# yielding 429 → 500 on /reports/data. A small inter-page sleep keeps
# us well under, and an exponential retry handles any spike.
_PAGE_SLEEP_SEC = 0.4
_RETRY_STATUSES = (429, 502, 503, 504)
_MAX_RETRIES = 6


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

    async def _get_with_retry(
        self, path: str, params: dict[str, Any],
    ) -> httpx.Response:
        """GET with exponential retry on 429/5xx. Honors Retry-After when set."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                r = await self._client.get(path, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code not in _RETRY_STATUSES:
                return r
            ra = r.headers.get("retry-after")
            try:
                wait = float(ra) if ra is not None else min(2 ** attempt, 8)
            except ValueError:
                wait = min(2 ** attempt, 8)
            logger.warning(
                "Twenty %s on %s — retry %d/%d in %.1fs",
                r.status_code, path, attempt + 1, _MAX_RETRIES, wait,
            )
            await asyncio.sleep(wait)
            last_exc = httpx.HTTPStatusError(
                f"{r.status_code} after retries", request=r.request, response=r,
            )
        if last_exc:
            raise last_exc
        raise RuntimeError(f"_get_with_retry: exhausted retries on {path}")

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
            r = await self._get_with_retry(path, params)
            r.raise_for_status()
            items = r.json().get("data", {}).get(plural, [])
            if not items:
                break
            out.extend(items)
            cursor = items[-1].get("createdAt")
            if len(items) < 100:
                break
            # Inter-page courtesy sleep — keeps us under the shared
            # 100/60s Twenty quota across all 4 endpoints we scan.
            await asyncio.sleep(_PAGE_SLEEP_SEC)
        return out

    async def load_calls_and_operators(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
        """Reduced load for the per-operator report — no timeline events."""
        crs = await self._page_all("/rest/callRecords", "callRecords")
        ops = await self._page_all("/rest/operators", "operators")
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
        return crs, ops, members_by_id

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
