"""Redis TTL cache for today-inclusive reports (TTL 5 min)."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from ..domain.models import EmployeeShare, LocationRepeatRow, ReportDTO, ReportScope

logger = logging.getLogger(__name__)

TTL_SECONDS = 300


def _cache_key(scope: ReportScope, user_id: int | None,
               from_ts: datetime, to_ts: datetime) -> str:
    return f"reports:{scope.value}:{user_id or 'all'}:{from_ts.isoformat()}:{to_ts.isoformat()}"


def _encode(dto: ReportDTO) -> str:
    data = asdict(dto)
    # datetimes → ISO strings
    data["period_from"] = dto.period_from.isoformat()
    data["period_to"] = dto.period_to.isoformat()
    data["scope"] = dto.scope.value
    # tuples → lists for JSON
    data["repeats_by_location"] = [asdict(r) for r in dto.repeats_by_location]
    data["share_per_user"] = [asdict(s) for s in dto.share_per_user]
    return json.dumps(data, ensure_ascii=False)


def _decode(raw: str) -> ReportDTO:
    data = json.loads(raw)
    return ReportDTO(
        scope=ReportScope(data["scope"]),
        period_from=datetime.fromisoformat(data["period_from"]),
        period_to=datetime.fromisoformat(data["period_to"]),
        user_id=data.get("user_id"),
        completed_tasks=data.get("completed_tasks", 0),
        total_duration_seconds=data.get("total_duration_seconds", 0),
        avg_duration_seconds=data.get("avg_duration_seconds"),
        complex_tasks=data.get("complex_tasks", 0),
        avg_complex_duration_seconds=data.get("avg_complex_duration_seconds"),
        repeats_count=data.get("repeats_count", 0),
        repeats_by_location=tuple(
            LocationRepeatRow(**r) for r in data.get("repeats_by_location", [])
        ),
        script_violations_first_call=data.get("script_violations_first_call", 0),
        avg_response_time_seconds=data.get("avg_response_time_seconds"),
        total_tasks=data.get("total_tasks", 0),
        share_per_user=tuple(
            EmployeeShare(**s) for s in data.get("share_per_user", [])
        ),
        pending_tasks=data.get("pending_tasks", 0),
    )


class ReportsCache:
    """Only caches reports that include today; historical reports are always cold."""

    def __init__(self, redis: Redis, ttl_seconds: int = TTL_SECONDS) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    def _cacheable(self, to_ts: datetime) -> bool:
        return to_ts.date() >= datetime.now(UTC).date()

    async def get(
        self, scope: ReportScope, user_id: int | None,
        from_ts: datetime, to_ts: datetime,
    ) -> ReportDTO | None:
        if not self._cacheable(to_ts):
            return None
        try:
            raw = await self._redis.get(_cache_key(scope, user_id, from_ts, to_ts))
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return _decode(raw)
        except Exception:
            logger.warning("ReportsCache get failed", exc_info=True)
            return None

    async def set(
        self, scope: ReportScope, user_id: int | None,
        from_ts: datetime, to_ts: datetime, dto: ReportDTO,
    ) -> None:
        if not self._cacheable(to_ts):
            return
        try:
            await self._redis.set(
                _cache_key(scope, user_id, from_ts, to_ts),
                _encode(dto),
                ex=self._ttl,
            )
        except Exception:
            logger.warning("ReportsCache set failed", exc_info=True)
