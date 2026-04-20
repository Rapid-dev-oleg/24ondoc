"""Telegram reports formatter — ReportDTO → monospace <pre> table.

Column widths are declared once in `_COLS` and used by both header and
body so nothing can drift. Telegram renders <pre> in a fixed-width font
— as long as each line has identical spacing, the table lines up cleanly
on mobile and desktop.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from reports.domain.models import EmployeeRow, ReportDTO

TG_MSG_LIMIT = 3900


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
    return f"{s // 86400}д{(s % 86400) // 3600:02d}ч"


def _fmt_int(n: int | None) -> str:
    return "—" if n is None else str(n)


def _fmt_period(dt_from: datetime, dt_to: datetime) -> str:
    f = dt_from.strftime("%d.%m")
    t = dt_to.strftime("%d.%m.%Y")
    return f if f == t else f"{f} — {t}"


def _short(name: str, width: int) -> str:
    return name if len(name) <= width else name[: width - 1] + "…"


# width, header, right_aligned, accessor
_COLS: tuple[tuple[int, str, bool, Callable[[EmployeeRow], str]], ...] = (
    (14, "Сотрудник", False, lambda r: _short(r.display_name, 14)),
    (4,  "Зав",      True,  lambda r: _fmt_int(r.completed)),
    (7,  "Время",    True,  lambda r: format_duration(r.total_duration_seconds)),
    (6,  "Ср",       True,  lambda r: format_duration(r.avg_duration_seconds)),
    (5,  "Слож",     True,  lambda r: _fmt_int(r.complex_count)),
    (6,  "Ср.сл",    True,  lambda r: format_duration(r.avg_complex_duration_seconds)),
    (4,  "Пвт",      True,  lambda r: _fmt_int(r.repeats_count)),
    (4,  "Нар",      True,  lambda r: _fmt_int(r.script_violations)),
    (4,  "Акт",      True,  lambda r: _fmt_int(r.pending_count)),
    (7,  "Ср.отв",   True,  lambda r: format_duration(r.avg_response_time_seconds)),
)


def _line(cells: list[tuple[int, str, bool]]) -> str:
    parts = []
    for width, text, right in cells:
        parts.append(f"{text:>{width}}" if right else f"{text:<{width}}")
    return " ".join(parts)


def _render_header() -> str:
    return _line([(w, h, r) for w, h, r, _ in _COLS])


def _render_row(row: EmployeeRow) -> str:
    return _line([(w, getter(row), r) for w, _, r, getter in _COLS])


def _render_sep() -> str:
    total = sum(w for w, _, _, _ in _COLS) + (len(_COLS) - 1)
    return "─" * total


def _render_table(rows: list[EmployeeRow], totals: EmployeeRow | None) -> str:
    lines = [_render_header(), _render_sep()]
    for r in rows:
        lines.append(_render_row(r))
    if totals is not None:
        lines.append(_render_sep())
        lines.append(_render_row(totals))
    return "\n".join(lines)


def _render_legend() -> str:
    return (
        "Зав — завершил, Ср — ср.время, Слож — сложных (выс./крит.),\n"
        "Ср.сл — ср.время на сложную, Пвт — повторных,\n"
        "Нар — нарушений скрипта, Акт — активных (не завершено),\n"
        "Ср.отв — ср. время реагирования."
    )


def format_report(dto: ReportDTO) -> str:
    header = (
        f"Период: {_fmt_period(dto.period_from, dto.period_to)}\n"
        f"Создано задач: {dto.total_created_in_period}\n"
    )
    if not dto.rows:
        return f"<pre>{header}\n(нет данных за период)</pre>"
    return f"<pre>{header}\n{_render_table(list(dto.rows), dto.totals)}\n\n{_render_legend()}</pre>"


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
