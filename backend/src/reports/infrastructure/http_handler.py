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

from reports.application.compute_operator_report import compute_operator_report
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


@router.get("/operators/data")
async def get_operators_data(
    request: Request,
    from_ts: str = Query(alias="from"),
    to_ts: str = Query(alias="to"),
) -> JSONResponse:
    try:
        from_dt = _parse_query_ts(from_ts)
        to_dt = _parse_query_ts(to_ts, end_of_day=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if to_dt < from_dt:
        raise HTTPException(status_code=400, detail="'to' must be >= 'from'")

    settings = request.app.state.settings
    async with TwentyTimelineReader(
        settings.twenty_base_url, settings.twenty_api_key,
    ) as reader:
        crs, ops, members_by_id = await reader.load_calls_and_operators()

    dto = compute_operator_report(
        call_records=crs,
        operators=ops,
        members_by_id=members_by_id,
        period_from=from_dt,
        period_to=to_dt,
    )
    payload = {
        "period_from": dto.period_from.isoformat(),
        "period_to": dto.period_to.isoformat(),
        "total_calls": dto.total_calls,
        "total_violations": dto.total_violations,
        "rows": [
            {
                "operator_id": r.operator_id,
                "display_name": r.display_name,
                "work_phone": r.work_phone,
                "member_name": r.member_name,
                "calls_count": r.calls_count,
                "calls_with_violations": r.calls_with_violations,
                "violations_total": r.violations_total,
                "avg_violations_per_call": r.avg_violations_per_call,
                "avg_duration_seconds": r.avg_duration_seconds,
                "top_missed_phrases": [
                    {"phrase": ph, "count": n} for ph, n in r.top_missed_phrases
                ],
            }
            for r in dto.rows
        ],
    }
    return JSONResponse(payload)


@router.get("/operators", response_class=HTMLResponse)
async def operators_page() -> HTMLResponse:
    today = datetime.now(MSK).date().isoformat()
    html = _OPERATORS_HTML.format(default_from=today, default_to=today)
    return HTMLResponse(html, headers={
        "Content-Security-Policy": "frame-ancestors *",
        "X-Frame-Options": "ALLOWALL",
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    })


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
  /* Spinner shown while /reports/data is loading. */
  #loader {{
    display: none;
    flex-direction: column; align-items: center; gap: 12px;
    padding: 48px 16px; color: var(--muted); font-size: 13px;
  }}
  #loader.show {{ display: flex; }}
  .spinner {{
    width: 36px; height: 36px; border-radius: 50%;
    border: 3px solid var(--border);
    border-top-color: var(--accent);
    animation: spin 0.8s linear infinite;
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  /* Summary cards — intake breakdown over the selected period. */
  .cards {{
    display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 16px;
  }}
  .card {{
    flex: 1 1 140px; min-width: 140px;
    padding: 14px 16px; border: 1px solid var(--border); border-radius: 8px;
    background: #fff; display: flex; flex-direction: column; gap: 4px;
  }}
  .card.accent {{ border-color: var(--accent); }}
  .card.success {{ border-color: #2ecc71; }}
  .card.warn {{ border-color: #e67e22; }}
  .card.muted {{ border-color: var(--border); background: var(--row-alt); }}
  .card-label {{
    font-size: 11px; color: var(--muted); text-transform: uppercase;
    letter-spacing: .04em;
  }}
  .card-value {{
    font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums;
  }}
  .card.success .card-value {{ color: #27ae60; }}
  .card.warn .card-value {{ color: #d35400; }}
  .card.accent .card-value {{ color: var(--accent); }}
  .period-line {{
    color: var(--muted); font-size: 12px; margin-bottom: 12px;
  }}
</style>
</head>
<body>
<h1>Отчёт по сотрудникам</h1>
<nav style="margin-bottom:8px"><a href="operators" style="color:var(--accent);text-decoration:none">→ Отчёт по операторам</a></nav>

<form id="f">
  <label>Период с<input type="date" name="from" value="{default_from}"></label>
  <label>по<input type="date" name="to" value="{default_to}"></label>
  <label>Сотрудник
    <select name="user_id"><option value="">— Все сотрудники —</option></select>
  </label>
  <button type="submit">Получить отчёт</button>
</form>

<div id="summary"></div>
<div id="loader"><div class="spinner"></div><div>Загружаем данные из Twenty…</div></div>
<div id="table"></div>
<div id="err"></div>

<script>
const form = document.getElementById('f');
const tableDiv = document.getElementById('table');
const summaryDiv = document.getElementById('summary');
const errDiv = document.getElementById('err');
const loaderDiv = document.getElementById('loader');
const userSelect = form.querySelector('select[name=user_id]');

function fmtMSKDate(iso) {{
  // Server returns period bounds in UTC; users picked MSK (Europe/Moscow,
  // UTC+3). Shift before slicing to YYYY-MM-DD so the displayed dates
  // match what the operator chose.
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso.slice(0, 10);
  const msk = new Date(d.getTime() + 3 * 3600 * 1000);
  return msk.toISOString().slice(0, 10);
}}

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
    ['Сотрудник', r => r.display_name,                            'name'],
    ['Завершил',  r => fmtInt(r.completed),                       'other'],
    ['Общее время', r => fmtDuration(r.total_duration_seconds),   'other'],
    ['Среднее',   r => fmtDuration(r.avg_duration_seconds),       'other'],
    ['Сложных',   r => fmtInt(r.complex_count),                   'other'],
    ['Ср.сложн',  r => fmtDuration(r.avg_complex_duration_seconds), 'other'],
    ['Повторных', r => fmtInt(r.repeats_count),                   'other'],
    // Нарушения скрипта НЕ показываем у сотрудников: они считаются по
    // оператору-приёмщику звонка, а это поле — assignee (технарь).
    // Эта статистика живёт на /reports/operators.
    ['Активных',  r => fmtInt(r.pending_count),                   'pending'],
    ['Ср.реаг.',  r => fmtDuration(r.avg_response_time_seconds),  'other'],
  ];
  const thead = '<tr>' + cols.map(c => '<th>' + c[0] + '</th>').join('') + '</tr>';
  const body = (dto.rows || []).map(r => {{
    // For the "— не назначено" bucket only the pending_count makes sense;
    // everything else is zero by definition (never assigned → never completed).
    const unassigned = r.user_id === null;
    return '<tr>' + cols.map(c => {{
      const v = unassigned && c[2] === 'other' ? '—' : (c[1](r) ?? '—');
      return '<td>' + v + '</td>';
    }}).join('') + '</tr>';
  }}).join('');
  const foot = (dto.totals && dto.scope !== 'employee') ? (
    '<tr>' + cols.map(c => '<td>' + (c[1](dto.totals) ?? '—') + '</td>').join('') + '</tr>'
  ) : '';
  tableDiv.innerHTML = '<table><thead>' + thead + '</thead><tbody>' + body
                     + '</tbody><tfoot>' + foot + '</tfoot></table>';
  const total = dto.total_created_in_period;
  const done  = dto.created_completed;
  const wip   = dto.created_in_progress;
  const unasg = dto.created_unassigned;
  summaryDiv.innerHTML =
      '<div class="period-line">Период: ' + fmtMSKDate(dto.period_from)
      + ' — ' + fmtMSKDate(dto.period_to) + '</div>'
    + '<div class="cards">'
    +   '<div class="card accent">'
    +     '<div class="card-label">Создано в периоде</div>'
    +     '<div class="card-value">' + fmtInt(total) + '</div>'
    +   '</div>'
    +   '<div class="card success">'
    +     '<div class="card-label">Выполнено</div>'
    +     '<div class="card-value">' + fmtInt(done) + '</div>'
    +   '</div>'
    +   '<div class="card">'
    +     '<div class="card-label">В работе</div>'
    +     '<div class="card-value">' + fmtInt(wip) + '</div>'
    +   '</div>'
    +   '<div class="card warn">'
    +     '<div class="card-label">Не назначено</div>'
    +     '<div class="card-value">' + fmtInt(unasg) + '</div>'
    +   '</div>'
    + '</div>';
}}

form.addEventListener('submit', async (e) => {{
  e.preventDefault();
  errDiv.textContent = '';
  tableDiv.innerHTML = '';
  summaryDiv.innerHTML = '';
  loaderDiv.classList.add('show');
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
    loaderDiv.classList.remove('show');
    btn.disabled = false; btn.textContent = 'Получить отчёт';
  }}
}});

loadMembers().then(() => form.requestSubmit());
</script>
</body>
</html>
"""


_OPERATORS_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Отчёт по операторам</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{
    --bg: #fff; --fg: #111; --muted: #888;
    --border: #e5e5e5; --row-alt: #fafafa; --accent: #2e7af0;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    margin: 0; padding: 16px; color: var(--fg); background: var(--bg);
    font-size: 14px; line-height: 1.4;
  }}
  h1 {{ font-size: 18px; margin: 0 0 12px; font-weight: 600; }}
  nav a {{ color: var(--accent); text-decoration: none; margin-right: 16px; }}
  form {{
    display: flex; gap: 8px; align-items: flex-end; flex-wrap: wrap;
    padding: 12px; background: var(--row-alt); border: 1px solid var(--border);
    border-radius: 6px; margin-bottom: 16px;
  }}
  form label {{ display: flex; flex-direction: column; font-size: 12px; color: var(--muted); }}
  form input {{
    padding: 6px 8px; border: 1px solid var(--border); border-radius: 4px;
    font-size: 14px; min-height: 32px;
  }}
  form button {{
    padding: 8px 16px; border: 0; border-radius: 4px; background: var(--accent);
    color: #fff; font-weight: 600; cursor: pointer; min-height: 32px;
  }}
  form button:disabled {{ opacity: .5; cursor: progress; }}
  #summary {{ color: var(--muted); font-size: 13px; margin-bottom: 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }}
  thead th {{
    text-align: right; padding: 8px 10px; font-size: 12px; font-weight: 600;
    color: var(--muted); border-bottom: 2px solid var(--border); white-space: nowrap;
    position: sticky; top: 0; background: var(--bg);
  }}
  thead th:first-child, thead th.left {{ text-align: left; }}
  tbody td {{
    padding: 8px 10px; border-bottom: 1px solid var(--border); text-align: right;
    white-space: nowrap; vertical-align: top;
  }}
  tbody td:first-child, tbody td.left {{ text-align: left; }}
  tbody tr:nth-child(odd) {{ background: var(--row-alt); }}
  .phr {{ font-size: 12px; color: #555; max-width: 360px; white-space: normal; }}
  .phr-line {{ display: block; }}
  .phr-cnt {{ color: var(--muted); margin-right: 6px; }}
  #err {{ color: #c0392b; margin-top: 8px; }}
  #loader {{
    display: none;
    flex-direction: column; align-items: center; gap: 12px;
    padding: 48px 16px; color: var(--muted); font-size: 13px;
  }}
  #loader.show {{ display: flex; }}
  .spinner {{
    width: 36px; height: 36px; border-radius: 50%;
    border: 3px solid var(--border);
    border-top-color: var(--accent);
    animation: spin 0.8s linear infinite;
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .cards {{
    display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 16px;
  }}
  .card {{
    flex: 1 1 140px; min-width: 140px;
    padding: 14px 16px; border: 1px solid var(--border); border-radius: 8px;
    background: #fff; display: flex; flex-direction: column; gap: 4px;
  }}
  .card.accent {{ border-color: var(--accent); }}
  .card.warn   {{ border-color: #e67e22; }}
  .card-label {{
    font-size: 11px; color: var(--muted); text-transform: uppercase;
    letter-spacing: .04em;
  }}
  .card-value {{
    font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums;
  }}
  .card.accent .card-value {{ color: var(--accent); }}
  .card.warn .card-value   {{ color: #d35400; }}
  .period-line {{
    color: var(--muted); font-size: 12px; margin-bottom: 12px;
  }}
</style>
</head>
<body>
<h1>Отчёт по операторам — нарушения скрипта на входящих звонках</h1>
<nav><a href="./">← К отчёту по сотрудникам</a></nav>

<form id="f">
  <label>Период с<input type="date" name="from" value="{default_from}"></label>
  <label>по<input type="date" name="to" value="{default_to}"></label>
  <button type="submit">Получить отчёт</button>
</form>

<div id="summary"></div>
<div id="loader"><div class="spinner"></div><div>Загружаем данные из Twenty…</div></div>
<div id="table"></div>
<div id="err"></div>

<script>
const form=document.getElementById('f');
const tableDiv=document.getElementById('table');
const summaryDiv=document.getElementById('summary');
const errDiv=document.getElementById('err');
const loaderDiv=document.getElementById('loader');

function fmtMSKDate(iso){{
  // Same UTC→MSK shift as on /reports — show the day the operator picked.
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso.slice(0, 10);
  const msk = new Date(d.getTime() + 3 * 3600 * 1000);
  return msk.toISOString().slice(0, 10);
}}
function fmtSec(s){{
  if(s===null||s===undefined) return '—';
  s=Math.round(s);
  if(s<60) return s+'с';
  if(s<3600) return Math.floor(s/60)+'м '+(s%60)+'с';
  return Math.floor(s/3600)+'ч '+Math.floor((s%3600)/60)+'м';
}}
function fmtFloat(x,d){{ return x==null?'—':x.toFixed(d??1); }}
function fmtInt(n){{ return n==null?'—':n.toLocaleString('ru-RU'); }}

function renderTable(dto){{
  const head =
    '<thead><tr>'
    + '<th class="left">Оператор</th>'
    + '<th class="left">Линия</th>'
    + '<th class="left">Сотрудник</th>'
    + '<th>Звонков</th>'
    + '<th>С нарушениями</th>'
    + '<th>% наруш.</th>'
    + '<th>Σ нарушений</th>'
    + '<th>Ср. на звонок</th>'
    + '<th>Ср. длит.</th>'
    + '<th class="left">Топ пропущенных фраз</th>'
    + '</tr></thead>';
  const rows=(dto.rows||[]).map(r=>{{
    const pct = r.calls_count
      ? Math.round(100*r.calls_with_violations/r.calls_count) + '%'
      : '—';
    const phrases = (r.top_missed_phrases||[])
      .map(p => '<span class="phr-line"><span class="phr-cnt">'+p.count+'×</span>'+p.phrase+'</span>')
      .join('');
    return '<tr>'
      + '<td class="left">'+r.display_name+'</td>'
      + '<td class="left">'+(r.work_phone||'—')+'</td>'
      + '<td class="left">'+(r.member_name||'<span style="color:#aaa">не привязан</span>')+'</td>'
      + '<td>'+fmtInt(r.calls_count)+'</td>'
      + '<td>'+fmtInt(r.calls_with_violations)+'</td>'
      + '<td>'+pct+'</td>'
      + '<td>'+fmtInt(r.violations_total)+'</td>'
      + '<td>'+fmtFloat(r.avg_violations_per_call,1)+'</td>'
      + '<td>'+fmtSec(r.avg_duration_seconds)+'</td>'
      + '<td class="left phr">'+phrases+'</td>'
      + '</tr>';
  }}).join('');
  tableDiv.innerHTML='<table>'+head+'<tbody>'+rows+'</tbody></table>';
  summaryDiv.innerHTML =
      '<div class="period-line">Период: ' + fmtMSKDate(dto.period_from)
      + ' — ' + fmtMSKDate(dto.period_to) + '</div>'
    + '<div class="cards">'
    +   '<div class="card accent">'
    +     '<div class="card-label">Звонков всего</div>'
    +     '<div class="card-value">' + fmtInt(dto.total_calls) + '</div>'
    +   '</div>'
    +   '<div class="card warn">'
    +     '<div class="card-label">Σ нарушений</div>'
    +     '<div class="card-value">' + fmtInt(dto.total_violations) + '</div>'
    +   '</div>'
    + '</div>';
}}

form.addEventListener('submit', async (e)=>{{
  e.preventDefault();
  errDiv.textContent='';
  tableDiv.innerHTML='';
  summaryDiv.innerHTML='';
  loaderDiv.classList.add('show');
  const fd=new FormData(form);
  const params=new URLSearchParams({{from:fd.get('from'),to:fd.get('to')}});
  const btn=form.querySelector('button');
  btn.disabled=true; btn.textContent='Загрузка…';
  try{{
    const r=await fetch('operators/data?'+params.toString());
    if(!r.ok) throw new Error('HTTP '+r.status+': '+(await r.text()));
    renderTable(await r.json());
  }}catch(ex){{ errDiv.textContent=String(ex); }}
  finally{{
    loaderDiv.classList.remove('show');
    btn.disabled=false; btn.textContent='Получить отчёт';
  }}
}});

form.requestSubmit();
</script>
</body>
</html>
"""
