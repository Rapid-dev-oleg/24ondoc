"""Pure function — INCOMING CallRecord set → per-operator metrics.

Why a separate report from compute_report.py: that one groups by Task
assigneeId (кто закрыл), this one groups by CallRecord operatorRelId
(кто принял звонок). Different question, different denominator.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from ..domain.models import OperatorReportDTO, OperatorRow


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def compute_operator_report(
    *,
    call_records: list[dict[str, Any]],
    operators: list[dict[str, Any]],
    members_by_id: dict[str, str],
    period_from: datetime,
    period_to: datetime,
    top_phrases_per_op: int = 5,
) -> OperatorReportDTO:
    """Aggregate INCOMING calls within [period_from, period_to] by operatorRelId."""

    # operator id → meta
    op_meta: dict[str, dict[str, Any]] = {}
    for o in operators:
        wp = (o.get("workPhone") or {})
        op_meta[o["id"]] = {
            "displayName": (o.get("displayName") or "?").strip(),
            "workPhone": (wp.get("primaryPhoneNumber") if isinstance(wp, dict) else None),
            "memberId": o.get("memberRelId"),
        }

    # Aggregation buckets
    Bucket = lambda: {
        "calls": 0,
        "calls_with_v": 0,
        "violations": 0,
        "duration_sum": 0,
        "duration_n": 0,
        "phrases": Counter(),
    }
    by_op: defaultdict[str, dict[str, Any]] = defaultdict(Bucket)

    total_calls = 0
    total_violations = 0
    for c in call_records:
        if c.get("direction") != "INCOMING":
            continue
        ts = _parse_iso(c.get("occurredAt"))
        if ts is None or not (period_from <= ts <= period_to):
            continue
        op_id = c.get("operatorRelId")
        if not op_id:
            continue  # unattached — skip the row entirely

        b = by_op[op_id]
        b["calls"] += 1
        total_calls += 1

        sv = int(c.get("scriptViolations") or 0)
        if sv > 0:
            b["calls_with_v"] += 1
            b["violations"] += sv
            total_violations += sv

        dur = c.get("duration")
        if isinstance(dur, (int, float)) and dur > 0:
            b["duration_sum"] += int(dur)
            b["duration_n"] += 1

        sm = (c.get("scriptMissing") or "").strip()
        if sm:
            for p in sm.split(";"):
                p = p.strip()
                if p:
                    b["phrases"][p] += 1

    rows: list[OperatorRow] = []
    for op_id, b in by_op.items():
        meta = op_meta.get(op_id, {})
        member_name: str | None = None
        mid = meta.get("memberId")
        if mid:
            member_name = members_by_id.get(mid)
        avg_v = (b["violations"] / b["calls"]) if b["calls"] else None
        avg_d = (b["duration_sum"] / b["duration_n"]) if b["duration_n"] else None
        rows.append(OperatorRow(
            operator_id=op_id,
            display_name=str(meta.get("displayName") or op_id[:8]),
            work_phone=meta.get("workPhone"),
            member_name=member_name,
            calls_count=b["calls"],
            calls_with_violations=b["calls_with_v"],
            violations_total=b["violations"],
            avg_violations_per_call=avg_v,
            avg_duration_seconds=avg_d,
            top_missed_phrases=tuple(b["phrases"].most_common(top_phrases_per_op)),
        ))
    rows.sort(key=lambda r: -r.calls_count)

    return OperatorReportDTO(
        period_from=period_from,
        period_to=period_to,
        rows=tuple(rows),
        total_calls=total_calls,
        total_violations=total_violations,
    )
