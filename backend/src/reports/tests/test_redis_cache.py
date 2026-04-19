"""Stage 8 — Reports Redis cache: only caches today-inclusive windows."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.reports.domain.models import ReportDTO, ReportScope
from src.reports.infrastructure.redis_cache import ReportsCache


def _dto(to_ts: datetime) -> ReportDTO:
    return ReportDTO(
        scope=ReportScope.SELF,
        period_from=to_ts - timedelta(days=7),
        period_to=to_ts,
        user_id=42,
        completed_tasks=5,
    )


@pytest.mark.asyncio
async def test_historical_report_is_not_cached() -> None:
    redis = MagicMock()
    redis.get = AsyncMock()
    redis.set = AsyncMock()
    cache = ReportsCache(redis=redis)

    historical_to = datetime.now(UTC) - timedelta(days=3)
    dto = _dto(historical_to)

    await cache.set(ReportScope.SELF, 42, dto.period_from, historical_to, dto)
    assert redis.set.await_count == 0

    out = await cache.get(ReportScope.SELF, 42, dto.period_from, historical_to)
    assert out is None
    assert redis.get.await_count == 0


@pytest.mark.asyncio
async def test_report_including_today_roundtrips() -> None:
    stored: dict[str, str] = {}

    async def _set(key, value, ex=None):
        stored[key] = value

    async def _get(key):
        return stored.get(key)

    redis = MagicMock()
    redis.set = AsyncMock(side_effect=_set)
    redis.get = AsyncMock(side_effect=_get)
    cache = ReportsCache(redis=redis)

    now = datetime.now(UTC).replace(microsecond=0)
    dto = _dto(now)
    await cache.set(ReportScope.SELF, 42, dto.period_from, dto.period_to, dto)

    assert redis.set.await_count == 1

    out = await cache.get(ReportScope.SELF, 42, dto.period_from, dto.period_to)
    assert out is not None
    assert out.completed_tasks == 5
    assert out.user_id == 42
    assert out.scope == ReportScope.SELF


@pytest.mark.asyncio
async def test_redis_errors_are_swallowed() -> None:
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=RuntimeError("redis down"))
    redis.set = AsyncMock(side_effect=RuntimeError("redis down"))
    cache = ReportsCache(redis=redis)

    now = datetime.now(UTC)
    dto = _dto(now)
    # Should not raise
    await cache.set(ReportScope.SELF, 42, dto.period_from, now, dto)
    out = await cache.get(ReportScope.SELF, 42, dto.period_from, now)
    assert out is None
