"""Stage 9 — Telegram formatter: DTO → <pre> HTML blocks."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.reports.domain.models import (
    EmployeeShare,
    LocationRepeatRow,
    ReportDTO,
    ReportScope,
)
from src.telegram_ingestion.infrastructure.reports_bot_handler import (
    parse_custom_period,
)
from src.telegram_ingestion.infrastructure.reports_formatter import (
    format_duration,
    format_report,
    split_for_telegram,
)


def _dto(scope: ReportScope, **over) -> ReportDTO:
    base = dict(
        scope=scope,
        period_from=datetime(2026, 4, 1, tzinfo=UTC),
        period_to=datetime(2026, 4, 19, 23, 59, 59, tzinfo=UTC),
        user_id=42 if scope == ReportScope.SELF else None,
        completed_tasks=12,
        total_duration_seconds=7200,
        avg_duration_seconds=600.0,
        complex_tasks=3,
        avg_complex_duration_seconds=900.0,
        repeats_count=2,
        script_violations_first_call=1,
        avg_response_time_seconds=45.0,
        total_tasks=50,
        pending_tasks=5,
    )
    base.update(over)
    return ReportDTO(**base)


def test_format_duration_levels() -> None:
    assert format_duration(0) == "0с"
    assert format_duration(45) == "45с"
    assert format_duration(90) == "1м 30с"
    assert format_duration(3600) == "1ч 00м"
    assert format_duration(3661) == "1ч 01м"
    assert format_duration(None) == "—"


def test_self_report_shows_personal_summary() -> None:
    dto = _dto(ReportScope.SELF)
    html = format_report(dto)
    assert html.startswith("<pre>")
    assert html.endswith("</pre>")
    assert "Всего в организации" in html
    assert "50" in html
    assert "Вы выполнили" in html
    assert "24.0%" in html  # 12 of 50
    assert "Незавершённых" in html
    # AGENT report shouldn't list other employees
    assert "Доля выполнения" not in html


def test_overall_report_includes_shares_and_repeats() -> None:
    dto = _dto(
        ReportScope.OVERALL,
        share_per_user=(
            EmployeeShare(user_id=1, display_name="Иван", completed=6, share_pct=50.0),
            EmployeeShare(user_id=2, display_name="Маша", completed=6, share_pct=50.0),
        ),
        repeats_by_location=(
            LocationRepeatRow(location_phone="79000000000", repeats=3),
            LocationRepeatRow(location_phone="79001112233", repeats=1),
        ),
    )
    html = format_report(dto)
    assert "Доля выполнения" in html
    assert "Иван" in html
    assert "Повторы по точкам" in html
    assert "79000000000" in html


def test_split_for_telegram_splits_long_reports() -> None:
    dto = _dto(
        ReportScope.OVERALL,
        share_per_user=tuple(
            EmployeeShare(user_id=i, display_name=f"User-{i}" * 3,
                          completed=1, share_pct=1.0)
            for i in range(300)
        ),
    )
    html = format_report(dto)
    chunks = split_for_telegram(html, limit=2000)
    assert len(chunks) > 1
    for c in chunks:
        assert c.startswith("<pre>") and c.endswith("</pre>")
        assert len(c) <= 2000


def test_parse_custom_period_accepts_short_dates() -> None:
    out = parse_custom_period("01.04 - 19.04")
    assert out is not None
    f, t = out
    assert f.day == 1 and f.month == 4
    assert t.day == 19 and t.month == 4
    assert t > f


def test_parse_custom_period_accepts_full_dates() -> None:
    out = parse_custom_period("01.04.2026 - 19.04.2026")
    assert out is not None
    f, _ = out
    assert f.year == 2026


def test_parse_custom_period_rejects_garbage() -> None:
    assert parse_custom_period("hello") is None
    assert parse_custom_period("01.04") is None
    assert parse_custom_period("19.04 - 01.04") is None  # end before start
