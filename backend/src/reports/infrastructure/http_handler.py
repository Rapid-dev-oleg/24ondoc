"""HTTP endpoints for the reports dashboard.

Three routes, mounted under /reports:
  GET /reports/          — HTML page (date range + employee select + table)
  GET /reports/members   — JSON list of workspace members for the <select>
  GET /reports/data      — JSON payload: same shape as ReportDTO

The HTML is self-contained (inline CSS/JS) so Twenty Dashboard can
`<iframe src="https://24ondoc.ru/reports/" />` and get a working widget.
No auth for now — the iframe sits inside the authenticated Twenty
workspace which already restricts access to that domain.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from reports.application.generate_report import GenerateReport
from reports.domain.models import ReportScope
from reports.infrastructure.twenty_timeline_reader import TwentyTimelineReader

router = APIRouter(prefix="/reports", tags=["reports"])


def _get_generator(request: Request) -> GenerateReport:
    gen = getattr(request.app.state, "generate_report", None)
    if gen is None:
        raise HTTPException(
            status_code=503,
            detail="reports generator not initialised",
        )
    return gen


@router.get("/members")
async def list_members(request: Request) -> JSONResponse:
    """Return workspace members for the report's employee dropdown.

    Reads live from Twenty so names/ids stay in sync without having to
    sync members into our DB.
    """
    settings = request.app.state.settings
    async with TwentyTimelineReader(
        settings.twenty_base_url, settings.twenty_api_key,
    ) as reader:
        data = await reader.load()
    members = [
        {"id": wmid, "name": name}
        for wmid, name in sorted(
            data.members_by_id.items(), key=lambda kv: kv[1].lower(),
        )
    ]
    return JSONResponse({"members": members})


@router.get("/data")
async def get_data(
    request: Request,
    from_ts: str = Query(alias="from"),
    to_ts: str = Query(alias="to"),
    user_id: str | None = Query(default=None),
) -> JSONResponse:
    try:
        from_dt = _parse_query_ts(from_ts)
        to_dt = _parse_query_ts(to_ts, end_of_day=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if to_dt < from_dt:
        raise HTTPException(status_code=400, detail="'to' must be >= 'from'")

    scope = ReportScope.EMPLOYEE if user_id else ReportScope.OVERALL
    gen = _get_generator(request)
    dto = await gen.execute(
        scope=scope, from_ts=from_dt, to_ts=to_dt, user_id=user_id or None,
    )
    # ReportDTO/EmployeeRow are plain dataclasses — asdict is enough.
    payload = asdict(dto)
    # enum → value; datetimes → isoformat
    payload["scope"] = dto.scope.value
    payload["period_from"] = dto.period_from.isoformat()
    payload["period_to"] = dto.period_to.isoformat()
    return JSONResponse(payload)


def _parse_query_ts(s: str, *, end_of_day: bool = False) -> datetime:
    """Accept 'YYYY-MM-DD' or full ISO-8601.

    Naive YYYY-MM-DD is interpreted as a Moscow calendar day (operators
    speak MSK, Twenty stores UTC — the converted window covers exactly
    the chosen MSK date).
    """
    if len(s) == 10:
        y, m, d = s.split("-")
        dt = datetime(int(y), int(m), int(d), tzinfo=MSK)
        if end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.astimezone(UTC)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@router.get("/", response_class=HTMLResponse)
async def report_page() -> HTMLResponse:
    today = datetime.now(MSK).date().isoformat()
    html = _REPORT_HTML.format(
        default_from=today,
        default_to=today,
    )
    return HTMLResponse(html, headers={
        # allow embedding from anywhere (Twenty Dashboard iframe)
        "Content-Security-Policy": "frame-ancestors *",
        "X-Frame-Options": "ALLOWALL",
        # don't cache the shell — iframes otherwise keep serving the
        # previous build until the tab is hard-reloaded
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    })


_REPORT_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Отчёт по сотрудникам</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{
    --bg: #fff;
    --fg: #111;
    --muted: #888;
    --border: #e5e5e5;
    --row-alt: #fafafa;
    --accent: #2e7af0;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    margin: 0; padding: 16px; color: var(--fg); background: var(--bg);
    font-size: 14px; line-height: 1.4;
  }}
  h1 {{ font-size: 18px; margin: 0 0 12px; font-weight: 600; }}
  form {{
    display: flex; gap: 8px; align-items: flex-end; flex-wrap: wrap;
    padding: 12px; background: var(--row-alt); border: 1px solid var(--border);
    border-radius: 6px; margin-bottom: 16px;
  }}
  form label {{
    display: flex; flex-direction: column; font-size: 12px; color: var(--muted);
  }}
  form input, form select {{
    padding: 6px 8px; border: 1px solid var(--border); border-radius: 4px;
    font-size: 14px; color: var(--fg); background: #fff; min-height: 32px;
  }}
  form button {{
    padding: 8px 16px; border: 0; border-radius: 4px; background: var(--accent);
    color: #fff; font-weight: 600; cursor: pointer; min-height: 32px;
  }}
  form button:disabled {{ opacity: .5; cursor: progress; }}
  #summary {{ color: var(--muted); font-size: 13px; margin-bottom: 8px; }}
  table {{
    width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums;
  }}
  thead th {{
    text-align: right; padding: 8px 10px; font-size: 12px; font-weight: 600;
    color: var(--muted); border-bottom: 2px solid var(--border);
    white-space: nowrap; position: sticky; top: 0; background: var(--bg);
  }}
  thead th:first-child {{ text-align: left; }}
  tbody td {{
    padding: 8px 10px; border-bottom: 1px solid var(--border); text-align: right;
    white-space: nowrap;
  }}
  tbody td:first-child {{ text-align: left; }}
  tbody tr:nth-child(odd) {{ background: var(--row-alt); }}
  tfoot td {{
    padding: 10px; font-weight: 600; border-top: 2px solid var(--border);
    text-align: right;
  }}
  tfoot td:first-child {{ text-align: left; }}
  #err {{ color: #c0392b; margin-top: 8px; }}
  .muted {{ color: var(--muted); }}
</style>
</head>
<body>
<h1>Отчёт по сотрудникам</h1>

<form id="f">
  <label>Период с<input type="date" name="from" value="{default_from}"></label>
  <label>по<input type="date" name="to" value="{default_to}"></label>
  <label>Сотрудник
    <select name="user_id"><option value="">— Все сотрудники —</option></select>
  </label>
  <button type="submit">Получить отчёт</button>
</form>

<div id="summary"></div>
<div id="table"></div>
<div id="err"></div>

<script>
const form = document.getElementById('f');
const tableDiv = document.getElementById('table');
const summaryDiv = document.getElementById('summary');
const errDiv = document.getElementById('err');
const userSelect = form.querySelector('select[name=user_id]');

function fmtDuration(sec) {{
  if (sec === null || sec === undefined) return '—';
  sec = Math.round(sec);
  if (sec < 60) return sec + 'с';
  if (sec < 3600) return Math.floor(sec/60) + 'м';
  if (sec < 86400) return Math.floor(sec/3600) + 'ч ' + Math.floor((sec%3600)/60) + 'м';
  const d = Math.floor(sec/86400);
  const h = Math.floor((sec%86400)/3600);
  return d + 'д ' + h + 'ч';
}}
function fmtInt(n) {{ return n == null ? '—' : n.toLocaleString('ru-RU'); }}

async function loadMembers() {{
  try {{
    const r = await fetch('members');
    const j = await r.json();
    j.members.forEach(m => {{
      const opt = document.createElement('option');
      opt.value = m.id; opt.textContent = m.name;
      userSelect.appendChild(opt);
    }});
  }} catch (e) {{
    console.error('members', e);
  }}
}}

function renderTable(dto) {{
  const cols = [
    ['Сотрудник', r => r.display_name],
    ['Завершил',  r => fmtInt(r.completed)],
    ['Общее время', r => fmtDuration(r.total_duration_seconds)],
    ['Среднее',   r => fmtDuration(r.avg_duration_seconds)],
    ['Сложных',   r => fmtInt(r.complex_count)],
    ['Ср.сложн',  r => fmtDuration(r.avg_complex_duration_seconds)],
    ['Повторных', r => fmtInt(r.repeats_count)],
    ['Нарушений', r => fmtInt(r.script_violations)],
    ['Активных',  r => fmtInt(r.pending_count)],
    ['Ср.реаг.',  r => fmtDuration(r.avg_response_time_seconds)],
  ];
  const thead = '<tr>' + cols.map(c => '<th>' + c[0] + '</th>').join('') + '</tr>';
  const body = (dto.rows || []).map(r =>
    '<tr>' + cols.map(c => '<td>' + (c[1](r) ?? '—') + '</td>').join('') + '</tr>'
  ).join('');
  const foot = (dto.totals && dto.scope !== 'employee') ? (
    '<tr>' + cols.map(c => '<td>' + (c[1](dto.totals) ?? '—') + '</td>').join('') + '</tr>'
  ) : '';
  tableDiv.innerHTML = '<table><thead>' + thead + '</thead><tbody>' + body
                     + '</tbody><tfoot>' + foot + '</tfoot></table>';
  summaryDiv.innerHTML = 'Создано задач в периоде: <b>' + dto.total_created_in_period
                      + '</b> · Период: ' + dto.period_from.slice(0,10)
                      + ' — ' + dto.period_to.slice(0,10);
}}

form.addEventListener('submit', async (e) => {{
  e.preventDefault();
  errDiv.textContent = '';
  const fd = new FormData(form);
  const params = new URLSearchParams({{
    from: fd.get('from'), to: fd.get('to'),
  }});
  if (fd.get('user_id')) params.set('user_id', fd.get('user_id'));
  const btn = form.querySelector('button');
  btn.disabled = true; btn.textContent = 'Загрузка…';
  try {{
    const r = await fetch('data?' + params.toString());
    if (!r.ok) throw new Error('HTTP ' + r.status + ': ' + (await r.text()));
    const dto = await r.json();
    renderTable(dto);
  }} catch (ex) {{
    errDiv.textContent = String(ex);
  }} finally {{
    btn.disabled = false; btn.textContent = 'Получить отчёт';
  }}
}});

loadMembers().then(() => form.requestSubmit());
</script>
</body>
</html>
"""
