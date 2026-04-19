"""Telegram reports formatter — DTO → monospace HTML <pre> blocks.

Lifted out of the bot handler so the formatting can be tested in
isolation and Telegram's 4096-char message limit is handled by the
`split_for_telegram` helper.
"""
from __future__ import annotations

from datetime import datetime

from reports.domain.models import EmployeeShare, LocationRepeatRow, ReportDTO, ReportScope

TG_MSG_LIMIT = 3900  # leave headroom for Telegram's 4096 ceiling


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}с"
    if s < 3600:
        return f"{s // 60}м {s % 60:02d}с"
    hours = s // 3600
    mins = (s % 3600) // 60
    return f"{hours}ч {mins:02d}м"


def _fmt_int(n: int | None) -> str:
    if n is None:
        return "—"
    # RU convention: non-breaking-ish space between thousands
    return f"{n:,}".replace(",", " ")


def _fmt_period(dt_from: datetime, dt_to: datetime) -> str:
    f = dt_from.strftime("%d.%m.%Y")
    t = dt_to.strftime("%d.%m.%Y")
    return f if f == t else f"{f} — {t}"


def _personal_block(dto: ReportDTO) -> str:
    percent = 0.0
    if dto.total_tasks:
        percent = round(100.0 * dto.completed_tasks / dto.total_tasks, 1)
    lines = [
        f"Период: {_fmt_period(dto.period_from, dto.period_to)}",
        "",
        f"  Всего в организации       : {_fmt_int(dto.total_tasks)}",
        f"  Вы выполнили              : {_fmt_int(dto.completed_tasks)}",
        f"  Ваш процент               : {percent}%",
        f"  Незавершённых (у вас)     : {_fmt_int(dto.pending_tasks)}",
        f"  Повторные обращения       : {_fmt_int(dto.repeats_count)}",
        f"  Ср. время на заявку       : {format_duration(dto.avg_duration_seconds)}",
        f"  Ср. время реагирования    : {format_duration(dto.avg_response_time_seconds)}",
        f"  Нарушений скрипта (1-й зв): {_fmt_int(dto.script_violations_first_call)}",
    ]
    return "\n".join(lines)


def _overall_block(dto: ReportDTO) -> str:
    lines = [
        f"Период: {_fmt_period(dto.period_from, dto.period_to)}",
        "",
        f"  Всего заявок              : {_fmt_int(dto.total_tasks)}",
        f"  Завершено                 : {_fmt_int(dto.completed_tasks)}",
        f"  Незавершённых             : {_fmt_int(dto.pending_tasks)}",
        f"  Сложных (high/urgent)     : {_fmt_int(dto.complex_tasks)}",
        f"  Общее время выполнения    : {format_duration(dto.total_duration_seconds)}",
        f"  Ср. время на заявку       : {format_duration(dto.avg_duration_seconds)}",
        f"  Ср. время на сложную      : {format_duration(dto.avg_complex_duration_seconds)}",
        f"  Ср. время реагирования    : {format_duration(dto.avg_response_time_seconds)}",
        f"  Нарушения скрипта         : {_fmt_int(dto.script_violations_first_call)}",
        f"  Повторные обращения       : {_fmt_int(dto.repeats_count)}",
    ]
    return "\n".join(lines)


def _share_block(shares: tuple[EmployeeShare, ...]) -> str:
    if not shares:
        return ""
    # Sort desc by share
    rows = sorted(shares, key=lambda s: s.share_pct, reverse=True)
    lines = ["", "Доля выполнения по сотрудникам:"]
    max_name = max(len(s.display_name) for s in rows)
    for s in rows:
        name = s.display_name.ljust(min(max_name, 20))
        lines.append(f"  {name}  {_fmt_int(s.completed):>4}  {s.share_pct:>5.1f}%")
    return "\n".join(lines)


def _repeats_block(rows: tuple[LocationRepeatRow, ...]) -> str:
    if not rows:
        return ""
    top = sorted(rows, key=lambda r: r.repeats, reverse=True)[:10]
    lines = ["", "Повторы по точкам (топ-10):"]
    for r in top:
        phone = r.location_phone[:16]
        lines.append(f"  {phone:<16}  {_fmt_int(r.repeats):>4}")
    return "\n".join(lines)


def format_report(dto: ReportDTO) -> str:
    if dto.scope == ReportScope.SELF:
        body = _personal_block(dto)
    else:
        body = _overall_block(dto)
        body += _share_block(dto.share_per_user)
        body += _repeats_block(dto.repeats_by_location)
    return f"<pre>{body}</pre>"


def split_for_telegram(html: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    """Split a <pre>…</pre> block into multiple <pre>…</pre> messages if needed."""
    if len(html) <= limit:
        return [html]
    # Strip wrapper, split by lines, rewrap
    inner = html
    if html.startswith("<pre>") and html.endswith("</pre>"):
        inner = html[len("<pre>"):-len("</pre>")]
    chunks: list[str] = []
    buf: list[str] = []
    used = 0
    frame = len("<pre></pre>")
    for line in inner.split("\n"):
        extra = len(line) + 1
        if used + extra + frame > limit and buf:
            chunks.append("<pre>" + "\n".join(buf) + "</pre>")
            buf = [line]
            used = extra
        else:
            buf.append(line)
            used += extra
    if buf:
        chunks.append("<pre>" + "\n".join(buf) + "</pre>")
    return chunks
