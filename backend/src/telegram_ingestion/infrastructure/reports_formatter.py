"""Telegram reports formatter — ReportDTO → monospace <pre> table.

One table per report, columns matching the per-operator spec; the last
row is the weighted "Итого". Telegram renders <pre> in a fixed-width
font, so the column alignment comes out clean on mobile too.

Wide displays show all columns; narrow ones may wrap — we keep names
short (3-4 chars abbreviations for headers) to fit 36-char Telegram
mobile width.
"""
from __future__ import annotations

from datetime import datetime

from reports.domain.models import EmployeeRow, ReportDTO, ReportScope

TG_MSG_LIMIT = 3900  # headroom under Telegram's 4096


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}с"
    if s < 3600:
        return f"{s // 60}м"
    if s < 86400:
        return f"{s // 3600}ч{(s % 3600) // 60:02d}"
    days = s // 86400
    hours = (s % 86400) // 3600
    return f"{days}д{hours:02d}ч"


def _fmt_int(n: int | None) -> str:
    if n is None:
        return "—"
    return f"{n}"


def _fmt_period(dt_from: datetime, dt_to: datetime) -> str:
    f = dt_from.strftime("%d.%m")
    t = dt_to.strftime("%d.%m.%Y")
    return f if f == t else f"{f} — {t}"


def _short(name: str, width: int) -> str:
    return name if len(name) <= width else name[: width - 1] + "…"


def _render_table(rows: list[EmployeeRow], totals: EmployeeRow | None) -> str:
    """Render the single unified table."""
    # Column widths (Telegram mobile ≈ 36 chars; trim name to fit).
    name_w = 14
    lines = [
        # header
        f"{'Сотрудник':<{name_w}} Зав   Время  Ср     Слож Пвт Нар Акт Ср.отв",
        "─" * (name_w + 43),
    ]
    for r in rows:
        lines.append(
            f"{_short(r.display_name, name_w):<{name_w}}"
            f" {r.completed:>3} "
            f"{format_duration(r.total_duration_seconds):>6} "
            f"{format_duration(r.avg_duration_seconds):>6} "
            f"{r.complex_count:>4} "
            f"{r.repeats_count:>3} "
            f"{r.script_violations:>3} "
            f"{r.pending_count:>3} "
            f"{format_duration(r.avg_response_time_seconds):>6}"
        )
    if totals is not None:
        lines.append("─" * (name_w + 43))
        lines.append(
            f"{'Итого':<{name_w}}"
            f" {totals.completed:>3} "
            f"{format_duration(totals.total_duration_seconds):>6} "
            f"{format_duration(totals.avg_duration_seconds):>6} "
            f"{totals.complex_count:>4} "
            f"{totals.repeats_count:>3} "
            f"{totals.script_violations:>3} "
            f"{totals.pending_count:>3} "
            f"{format_duration(totals.avg_response_time_seconds):>6}"
        )
    return "\n".join(lines)


def _render_legend() -> str:
    return (
        "Зав — завершил, Ср — среднее, Слож — сложных, Пвт — повторных,\n"
        "Нар — нарушений скрипта, Акт — активных (не завершено),\n"
        "Ср.отв — среднее время реагирования."
    )


def format_report(dto: ReportDTO) -> str:
    header = (
        f"Период: {_fmt_period(dto.period_from, dto.period_to)}\n"
        f"Создано задач: {dto.total_created_in_period}\n"
    )
    if not dto.rows:
        body = header + "\n(нет данных за период)"
        return f"<pre>{body}</pre>"
    table = _render_table(list(dto.rows), dto.totals)
    legend = _render_legend()
    return f"<pre>{header}\n{table}\n\n{legend}</pre>"


def split_for_telegram(html: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    if len(html) <= limit:
        return [html]
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
