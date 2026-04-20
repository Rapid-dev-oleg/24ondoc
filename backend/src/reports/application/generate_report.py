"""Reports orchestrator — TimelineReader + compute_report + Redis cache.

The SQL-based version has been dropped along with the local `task_events`
table; we now read directly from Twenty's built-in timelineActivity feed
(which the CRM itself populates on every CRUD, including a structured
diff in `properties.diff`).

Cache: redis key reports:{scope}:{user_id?}:{from}:{to}, TTL 5 min. We
only cache a window if `to_ts` is today — closed past periods could be
cached longer but we keep it simple.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from ..domain.models import EmployeeRow, ReportDTO, ReportScope
from ..infrastructure.twenty_timeline_reader import TwentyTimelineReader
from .compute_report import compute_report

logger = logging.getLogger(__name__)


class GenerateReport:
    def __init__(
        self,
        twenty_base_url: str,
        twenty_api_key: str,
        redis: Redis | None = None,
        cache_ttl_seconds: int = 300,
    ) -> None:
        self._twenty_base_url = twenty_base_url
        self._twenty_api_key = twenty_api_key
        self._redis = redis
        self._cache_ttl = cache_ttl_seconds

    async def list_members(self) -> dict[str, str]:
        """Return workspaceMemberId → display name from Twenty."""
        async with TwentyTimelineReader(
            self._twenty_base_url, self._twenty_api_key,
        ) as reader:
            data = await reader.load()
        return dict(data.members_by_id)

    async def execute(
        self,
        *,
        scope: ReportScope,
        from_ts: datetime,
        to_ts: datetime,
        user_id: str | None = None,
    ) -> ReportDTO:
        cache_key = self._cache_key(scope, user_id, from_ts, to_ts)
        if self._redis and self._cacheable(to_ts):
            try:
                raw = await self._redis.get(cache_key)
                if raw:
                    return _dto_from_json(raw.decode() if isinstance(raw, bytes) else raw)
            except Exception:
                logger.warning("reports cache read failed", exc_info=True)

        async with TwentyTimelineReader(
            self._twenty_base_url, self._twenty_api_key,
        ) as reader:
            data = await reader.load()

        dto = compute_report(
            data, from_ts=from_ts, to_ts=to_ts, scope=scope, user_id=user_id,
        )

        if self._redis and self._cacheable(to_ts):
            try:
                await self._redis.set(
                    cache_key, _dto_to_json(dto).encode(), ex=self._cache_ttl,
                )
            except Exception:
                logger.warning("reports cache write failed", exc_info=True)
        return dto

    @staticmethod
    def _cache_key(
        scope: ReportScope, user_id: str | None,
        from_ts: datetime, to_ts: datetime,
    ) -> str:
        return (
            f"reports:{scope.value}:{user_id or '-'}"
            f":{from_ts.isoformat()}:{to_ts.isoformat()}"
        )

    @staticmethod
    def _cacheable(to_ts: datetime) -> bool:
        # Only cache when window includes today — past closed windows could
        # be cached indefinitely but we keep the policy uniform.
        return to_ts.date() >= datetime.now(UTC).date()


# ---------- JSON codec for cache ----------

def _dto_to_json(dto: ReportDTO) -> str:
    def row_to_dict(r: EmployeeRow | None) -> dict[str, Any] | None:
        if r is None:
            return None
        return r.__dict__.copy()

    payload = {
        "scope": dto.scope.value,
        "period_from": dto.period_from.isoformat(),
        "period_to": dto.period_to.isoformat(),
        "user_id": dto.user_id,
        "rows": [row_to_dict(r) for r in dto.rows],
        "totals": row_to_dict(dto.totals),
        "total_created_in_period": dto.total_created_in_period,
    }
    return json.dumps(payload, ensure_ascii=False)


def _dto_from_json(s: str) -> ReportDTO:
    d = json.loads(s)

    def row_from_dict(raw: dict[str, Any] | None) -> EmployeeRow | None:
        if raw is None:
            return None
        return EmployeeRow(**raw)

    return ReportDTO(
        scope=ReportScope(d["scope"]),
        period_from=datetime.fromisoformat(d["period_from"]),
        period_to=datetime.fromisoformat(d["period_to"]),
        user_id=d.get("user_id"),
        rows=tuple(row_from_dict(r) for r in d.get("rows", []) if r is not None),
        totals=row_from_dict(d.get("totals")),
        total_created_in_period=int(d.get("total_created_in_period", 0)),
    )
