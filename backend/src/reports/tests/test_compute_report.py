"""Unit tests for reports.compute_report.

Synthetic TimelineData → ReportDTO. Verifies per-operator aggregation,
duration from `received_at`, the totals footer, and edge cases.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reports.application.compute_report import compute_report
from reports.domain.models import ReportScope
from reports.infrastructure.twenty_timeline_reader import TimelineData


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _tu_event(tid: str, happens_at: datetime, diff: dict, wmid: str | None = None) -> dict:
    return {
        "targetTaskId": tid,
        "happensAt": _iso(happens_at),
        "createdAt": _iso(happens_at),
        "workspaceMemberId": wmid,
        "properties": {"diff": diff},
    }


def _tc_event(tid: str, happens_at: datetime) -> dict:
    """task.created timeline event — carries the real Twenty INSERT timestamp."""
    return {
        "targetTaskId": tid,
        "name": "task.created",
        "happensAt": _iso(happens_at),
        "createdAt": _iso(happens_at),
        "workspaceMemberId": None,
        "properties": {},
    }


WM_NADYA = "wm-nadya"
WM_VOVA = "wm-vova"


def test_single_completion_with_reassignment() -> None:
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)

    t_created = datetime(2026, 4, 10, 10, 0, tzinfo=UTC)
    t_received = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)   # Vova picks it up
    t_completed = datetime(2026, 4, 11, 13, 0, tzinfo=UTC)  # 1h later

    tasks = ({
        "id": "t1",
        "createdAt": _iso(t_created),
        "assigneeId": WM_VOVA,
        "status": "VYPOLNENO",
        "vazhnost": "SREDNYAYA",
        "povtornoeObrashchenie": False,
        "scriptViolations": None,
    },)
    events = (
        _tu_event("t1", t_received, {"assigneeId": {"before": None, "after": WM_VOVA}}),
        _tu_event("t1", t_received, {"status": {"before": "TODO", "after": "V_RABOTE"}}, wmid=WM_VOVA),
        _tu_event("t1", t_completed, {"status": {"before": "V_RABOTE", "after": "VYPOLNENO"}}, wmid=WM_VOVA),
    )
    data = TimelineData(
        updated_events=events, tasks=tasks,
        members_by_id={WM_VOVA: "Вова Петров"},
    )
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)

    assert dto.totals is not None
    assert dto.totals.completed == 1
    # 1h spent in V_RABOTE (12:00→13:00), not wall-clock from creation
    assert dto.totals.total_duration_seconds == 3600
    rows = {r.user_id: r for r in dto.rows}
    assert WM_VOVA in rows
    assert rows[WM_VOVA].completed == 1
    assert rows[WM_VOVA].total_duration_seconds == 3600
    assert rows[WM_VOVA].display_name == "Вова Петров"


def test_complex_counts_by_category_not_importance() -> None:
    """«Сложные» определяются по КАТЕГОРИИ, не по важности (vazhnost).

    tA: kategoriya=INVENTARIZACIYA, низкая важность → сложная.
    tB: kategoriya=EGAIS, важность KRITICHNO → НЕ сложная.
    Доказывает, что метрика переехала с vazhnost на kategoriya.
    """
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)

    received = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    a_done = datetime(2026, 4, 11, 13, 0, tzinfo=UTC)   # 1h — сложная
    b_done = datetime(2026, 4, 11, 12, 30, tzinfo=UTC)  # 30m — не сложная

    tasks = (
        {
            "id": "tA", "createdAt": _iso(received),
            "assigneeId": WM_VOVA, "status": "VYPOLNENO",
            "kategoriya": "INVENTARIZACIYA", "vazhnost": "NIZKAYA",
        },
        {
            "id": "tB", "createdAt": _iso(received),
            "assigneeId": WM_VOVA, "status": "VYPOLNENO",
            "kategoriya": "EGAIS", "vazhnost": "KRITICHNO",
        },
    )
    events = (
        _tu_event("tA", received, {"assigneeId": {"before": None, "after": WM_VOVA}}),
        _tu_event("tA", received, {"status": {"before": "TODO", "after": "V_RABOTE"}}, wmid=WM_VOVA),
        _tu_event("tA", a_done, {"status": {"before": "V_RABOTE", "after": "VYPOLNENO"}}, wmid=WM_VOVA),
        _tu_event("tB", received, {"assigneeId": {"before": None, "after": WM_VOVA}}),
        _tu_event("tB", received, {"status": {"before": "TODO", "after": "V_RABOTE"}}, wmid=WM_VOVA),
        _tu_event("tB", b_done, {"status": {"before": "V_RABOTE", "after": "VYPOLNENO"}}, wmid=WM_VOVA),
    )
    data = TimelineData(
        updated_events=events, tasks=tasks,
        members_by_id={WM_VOVA: "Вова Петров"},
    )
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)

    row = next(r for r in dto.rows if r.user_id == WM_VOVA)
    assert row.completed == 2
    assert row.complex_count == 1                      # только INVENTARIZACIYA
    assert row.avg_complex_duration_seconds == 3600    # длительность tA, не tB
    assert dto.totals is not None
    assert dto.totals.complex_count == 1


def test_totals_pending_excludes_unassigned() -> None:
    """«Активных» в «Итого» считает только назначенные — как в таблице.

    Неназначенные открытые задачи в таблицу по сотрудникам не попадают,
    поэтому и в итог не суммируются (иначе 14 ≠ 1+2). Они видны отдельно
    как created_unassigned в summary.
    """
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)
    created = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)

    tasks = (
        {"id": "tp1", "createdAt": _iso(created),
         "assigneeId": WM_VOVA, "status": "V_RABOTE"},          # назначена, открыта
        {"id": "tp2", "createdAt": _iso(created),
         "assigneeId": None, "status": "TODO"},                 # НЕ назначена, открыта
        {"id": "tp3", "createdAt": _iso(created),
         "assigneeId": None, "status": "TODO"},                 # ещё одна неназначенная
    )
    data = TimelineData(
        updated_events=(), tasks=tasks, members_by_id={WM_VOVA: "Вова Петров"},
    )
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)

    # В таблице нет строки «не назначено»
    assert all(r.user_id is not None for r in dto.rows)
    # «Итого: Активных» = только назначенные (1), не 3
    assert dto.totals is not None
    assert dto.totals.pending_count == 1
    # Неназначенные за период видны отдельной метрикой
    assert dto.created_unassigned == 2


def test_atomic_close_counts_as_work_floor() -> None:
    """Закрытие БЕЗ «В работе» (TODO→Выполнено напрямую) = WORK_FLOOR (5 мин).

    Раньше такую задачу молча выкидывали (нет события назначения → skip),
    из-за чего «Завершил» не сходился с «Выполнено». Теперь она считается:
    оператор решил прямо на звонке → засчитываем 5 минут (звонок + клик),
    а не время её лежания в бэклоге.
    """
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)

    t_created = datetime(2026, 4, 5, 9, 0, tzinfo=UTC)
    t_completed = datetime(2026, 4, 5, 10, 30, tzinfo=UTC)  # 1.5h wall-clock

    tasks = ({
        "id": "t2", "createdAt": _iso(t_created),
        "assigneeId": WM_NADYA, "status": "VYPOLNENO",
        "vazhnost": "VYSOKAYA", "scriptViolationsTotal": 2,
    },)
    events = (
        _tu_event("t2", t_completed, {"status": {"before": "TODO", "after": "VYPOLNENO"}}, wmid=WM_NADYA),
    )
    data = TimelineData(events, tasks, members_by_id={WM_NADYA: "Надя"})

    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    row = next(r for r in dto.rows if r.user_id == WM_NADYA)
    assert row.completed == 1
    assert row.total_duration_seconds == 300        # 5-мин флор, не 1.5ч wall-clock
    assert row.avg_duration_seconds == 300
    assert row.complex_count == 0                   # vazhnost не влияет, kategoriya пустая
    assert row.script_violations == 2


def test_work_duration_sums_only_v_rabote_excluding_pause() -> None:
    """Длительность = сумма интервалов «В работе», паузы не считаются."""
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)

    t0 = datetime(2026, 4, 10, 10, 0, tzinfo=UTC)
    in_work = datetime(2026, 4, 10, 10, 0, tzinfo=UTC)   # → В работе
    pause = datetime(2026, 4, 10, 10, 30, tzinfo=UTC)    # 30м работы → пауза
    resume = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)    # 1.5ч паузы → снова в работу
    done = datetime(2026, 4, 10, 12, 20, tzinfo=UTC)     # +20м → Выполнено

    tasks = ({
        "id": "tw", "createdAt": _iso(t0),
        "assigneeId": WM_VOVA, "status": "VYPOLNENO",
    },)
    events = (
        _tu_event("tw", in_work, {"status": {"before": "TODO", "after": "V_RABOTE"}}, wmid=WM_VOVA),
        _tu_event("tw", pause, {"status": {"before": "V_RABOTE", "after": "PRIOSTANOVLENO"}}, wmid=WM_VOVA),
        _tu_event("tw", resume, {"status": {"before": "PRIOSTANOVLENO", "after": "V_RABOTE"}}, wmid=WM_VOVA),
        _tu_event("tw", done, {"status": {"before": "V_RABOTE", "after": "VYPOLNENO"}}, wmid=WM_VOVA),
    )
    data = TimelineData(events, tasks, members_by_id={WM_VOVA: "Вова"})
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    row = next(r for r in dto.rows if r.user_id == WM_VOVA)
    # 30м + 20м = 50м работы; 1.5ч паузы НЕ считаются
    assert row.total_duration_seconds == 50 * 60


def test_completion_without_status_event_excluded() -> None:
    """Task in VYPOLNENO state but timeline has NO status→VYPOLNENO event → skip.

    Covers legacy tasks where the status was written directly to DB without
    emitting a timelineActivity. Inflated durations come from pretending we
    closed such tasks "just now" — refuse to guess.
    """
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)

    t_created = datetime(2026, 4, 5, 9, 0, tzinfo=UTC)
    tasks = ({
        "id": "t3", "createdAt": _iso(t_created),
        "assigneeId": WM_NADYA, "status": "VYPOLNENO",
    },)
    # Only an assignment event, NO status transition.
    events = (
        _tu_event("t3", t_created + timedelta(minutes=10),
                  {"assigneeId": {"before": None, "after": WM_NADYA}}),
    )
    data = TimelineData(events, tasks, members_by_id={WM_NADYA: "Надя"})
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    assert dto.totals is not None
    assert dto.totals.completed == 0


def test_totals_are_weighted_not_mean_of_means() -> None:
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)
    base = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)

    # Each task: → В работе at `base`, → Выполнено N seconds later.
    # Vova: 1 task × 60s in-work; Nadya: 3 × 1200s → total 3660s; weighted avg = 915.
    tasks = (
        {"id": "v", "createdAt": _iso(base), "assigneeId": WM_VOVA, "status": "VYPOLNENO"},
        {"id": "n1", "createdAt": _iso(base), "assigneeId": WM_NADYA, "status": "VYPOLNENO"},
        {"id": "n2", "createdAt": _iso(base), "assigneeId": WM_NADYA, "status": "VYPOLNENO"},
        {"id": "n3", "createdAt": _iso(base), "assigneeId": WM_NADYA, "status": "VYPOLNENO"},
    )
    events = (
        _tu_event("v",  base, {"status": {"before": "TODO", "after": "V_RABOTE"}}),
        _tu_event("n1", base, {"status": {"before": "TODO", "after": "V_RABOTE"}}),
        _tu_event("n2", base, {"status": {"before": "TODO", "after": "V_RABOTE"}}),
        _tu_event("n3", base, {"status": {"before": "TODO", "after": "V_RABOTE"}}),
        _tu_event("v",  base + timedelta(seconds=60),   {"status": {"before": "V_RABOTE", "after": "VYPOLNENO"}}),
        _tu_event("n1", base + timedelta(seconds=1200), {"status": {"before": "V_RABOTE", "after": "VYPOLNENO"}}),
        _tu_event("n2", base + timedelta(seconds=1200), {"status": {"before": "V_RABOTE", "after": "VYPOLNENO"}}),
        _tu_event("n3", base + timedelta(seconds=1200), {"status": {"before": "V_RABOTE", "after": "VYPOLNENO"}}),
    )
    data = TimelineData(events, tasks, members_by_id={WM_VOVA: "Vova", WM_NADYA: "Nadya"})
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    assert dto.totals is not None
    assert dto.totals.completed == 4
    assert dto.totals.total_duration_seconds == 3660
    assert dto.totals.avg_duration_seconds == 915.0  # weighted, not (60+1200)/2


def test_completion_outside_window_excluded() -> None:
    from_ts = datetime(2026, 4, 10, tzinfo=UTC)
    to_ts = datetime(2026, 4, 20, tzinfo=UTC)

    t_before = datetime(2026, 4, 5, tzinfo=UTC)  # before window
    tasks = ({"id": "t", "createdAt": _iso(t_before), "assigneeId": WM_VOVA,
              "status": "VYPOLNENO"},)
    events = (
        _tu_event("t", t_before + timedelta(hours=1),
                  {"status": {"before": "TODO", "after": "VYPOLNENO"}}),
    )
    data = TimelineData(events, tasks, members_by_id={})
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    assert dto.totals is not None
    assert dto.totals.completed == 0


def test_pending_snapshot_counts_only_in_work() -> None:
    # «Активных» = снимок задач именно В РАБОТЕ (V_RABOTE), не window-scoped.
    # TODO (не взята) и VYPOLNENO (закрыта) не считаются.
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)
    tasks = (
        {"id": "a", "createdAt": _iso(from_ts), "assigneeId": WM_VOVA, "status": "V_RABOTE"},
        {"id": "b", "createdAt": _iso(from_ts), "assigneeId": WM_NADYA, "status": "TODO"},
        {"id": "c", "createdAt": _iso(from_ts), "assigneeId": WM_NADYA, "status": "VYPOLNENO"},
        {"id": "d", "createdAt": _iso(from_ts), "assigneeId": WM_NADYA, "status": "PRIOSTANOVLENO"},
    )
    data = TimelineData((), tasks, members_by_id={WM_VOVA: "V", WM_NADYA: "N"})
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    by_user = {r.user_id: r.pending_count for r in dto.rows}
    assert by_user[WM_VOVA] == 1            # V_RABOTE
    assert by_user[WM_NADYA] == 0           # TODO/VYPOLNENO/PRIOSTANOVLENO не в работе
    assert dto.totals is not None
    assert dto.totals.pending_count == 1


def test_scope_self_filters_rows() -> None:
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)
    tasks = (
        {"id": "a", "createdAt": _iso(from_ts), "assigneeId": WM_VOVA, "status": "VYPOLNENO"},
        {"id": "b", "createdAt": _iso(from_ts), "assigneeId": WM_NADYA, "status": "VYPOLNENO"},
    )
    events = (
        _tu_event("a", from_ts, {"assigneeId": {"before": None, "after": WM_VOVA}}),
        _tu_event("b", from_ts, {"assigneeId": {"before": None, "after": WM_NADYA}}),
        _tu_event("a", from_ts + timedelta(hours=1),
                  {"status": {"before": "TODO", "after": "VYPOLNENO"}}),
        _tu_event("b", from_ts + timedelta(hours=2),
                  {"status": {"before": "TODO", "after": "VYPOLNENO"}}),
    )
    data = TimelineData(events, tasks, members_by_id={WM_VOVA: "V", WM_NADYA: "N"})
    dto = compute_report(
        data, from_ts=from_ts, to_ts=to_ts,
        scope=ReportScope.SELF, user_id=WM_VOVA,
    )
    assert len(dto.rows) == 1
    assert dto.rows[0].user_id == WM_VOVA
    # totals still reflect the whole org, not the filtered row
    assert dto.totals is not None
    assert dto.totals.completed == 2


def test_response_time_uses_task_created_event_not_column() -> None:
    """M8 measures first-assignment − task.created event, NOT − task.createdAt.

    Our backend backfills task.createdAt to the ATS call time (days before
    the real Twenty INSERT), so using the column would conflate CRM-entry
    delay with operator reaction. Task.created timeline event is the
    trustworthy anchor.
    """
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)

    # Column says 04-05 (call date), but Twenty actually inserted 04-10;
    # assignee picked it up 5 minutes after the real insert.
    backfilled_created = datetime(2026, 4, 5, 9, 0, tzinfo=UTC)
    real_insert = datetime(2026, 4, 10, 14, 0, tzinfo=UTC)
    first_assign = real_insert + timedelta(minutes=5)

    tasks = ({
        "id": "t", "createdAt": _iso(backfilled_created),
        "assigneeId": WM_VOVA, "status": "TODO",
    },)
    updated = (_tu_event("t", first_assign,
                         {"assigneeId": {"before": None, "after": WM_VOVA}}),)
    created = (_tc_event("t", real_insert),)
    data = TimelineData(updated, tasks, members_by_id={WM_VOVA: "V"},
                        created_events=created)
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts)
    row = next(r for r in dto.rows if r.user_id == WM_VOVA)
    # 5 minutes, NOT 5 days + 5 minutes
    assert row.avg_response_time_seconds == 5 * 60


def test_response_time_task_without_created_event_skipped() -> None:
    """No task.created in timeline → response time can't be trusted → skip."""
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)

    tasks = ({
        "id": "t", "createdAt": _iso(from_ts),
        "assigneeId": WM_VOVA, "status": "TODO",
    },)
    updated = (_tu_event("t", from_ts + timedelta(minutes=10),
                         {"assigneeId": {"before": None, "after": WM_VOVA}}),)
    data = TimelineData(updated, tasks, members_by_id={WM_VOVA: "V"})
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts)
    row = next(r for r in dto.rows if r.user_id == WM_VOVA)
    assert row.avg_response_time_seconds is None


def test_atomic_save_does_not_use_task_lifetime() -> None:
    """«Назначить себе + выполнено» в одном save (без «В работе») НЕ должно
    давать время жизни задачи в CRM. Раньше fallback на task.created считал
    8.5 минут (от вставки до закрытия) — а при бэклоге это были бы дни.
    Теперь такое закрытие = WORK_FLOOR (5 мин), независимо от возраста задачи.
    """
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)

    real_insert = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
    atomic_close = real_insert + timedelta(minutes=8, seconds=30)

    tasks = ({
        "id": "atom", "createdAt": _iso(real_insert),
        "assigneeId": WM_VOVA, "status": "VYPOLNENO",
    },)
    updated = (_tu_event("atom", atomic_close, {
        "status": {"before": "TODO", "after": "VYPOLNENO"},
        "assigneeId": {"before": None, "after": WM_VOVA},
    }, wmid=WM_VOVA),)
    created = (_tc_event("atom", real_insert),)
    data = TimelineData(updated, tasks, members_by_id={WM_VOVA: "V"},
                        created_events=created)

    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts)
    row = next(r for r in dto.rows if r.user_id == WM_VOVA)
    assert row.completed == 1
    assert row.total_duration_seconds == 300  # 5-мин флор, НЕ 8.5 мин жизни


def test_duration_uses_latest_completion_not_first() -> None:
    """Task closed → reopened → closed again (in window). Duration must use
    the LAST VYPOLNENO event, not the first — the first one was reverted
    and doesn't represent real completion.
    """
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)
    base = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)

    tasks = ({
        "id": "rc", "createdAt": _iso(base),
        "assigneeId": WM_VOVA, "status": "VYPOLNENO",
    },)
    updated = (
        _tu_event("rc", base + timedelta(minutes=1),
                  {"status": {"before": "TODO", "after": "V_RABOTE"}}),
        _tu_event("rc", base + timedelta(minutes=5),
                  {"status": {"before": "V_RABOTE", "after": "VYPOLNENO"}}),   # 1-я закрытие: 4м работы
        _tu_event("rc", base + timedelta(minutes=10),
                  {"status": {"before": "VYPOLNENO", "after": "TODO"}}),       # переоткрыли
        _tu_event("rc", base + timedelta(minutes=20),
                  {"status": {"before": "TODO", "after": "V_RABOTE"}}),
        _tu_event("rc", base + timedelta(minutes=30),
                  {"status": {"before": "V_RABOTE", "after": "VYPOLNENO"}}),   # 2-я закрытие: +10м работы
    )
    created = (_tc_event("rc", base),)
    data = TimelineData(updated, tasks, members_by_id={WM_VOVA: "V"},
                        created_events=created)
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts)
    row = next(r for r in dto.rows if r.user_id == WM_VOVA)
    # Берём ПОСЛЕДНЕЕ закрытие (t+30m) → суммарное время в работе =
    # 4м (первый заход) + 10м (второй) = 14 минут.
    assert row.completed == 1
    assert row.total_duration_seconds == 14 * 60


def test_reopened_task_drops_out_of_completed() -> None:
    """Task went TODO→VYPOLNENO (in window) and was later reverted to TODO.
    Current status is non-terminal, so it must NOT appear as completed.
    """
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)
    base = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)

    tasks = ({
        "id": "r", "createdAt": _iso(base),
        "assigneeId": WM_VOVA, "status": "TODO",  # reverted back
    },)
    updated = (
        _tu_event("r", base + timedelta(minutes=2),
                  {"assigneeId": {"before": None, "after": WM_VOVA}}),
        _tu_event("r", base + timedelta(minutes=5),
                  {"status": {"before": "TODO", "after": "VYPOLNENO"}}),
        _tu_event("r", base + timedelta(minutes=10),
                  {"status": {"before": "VYPOLNENO", "after": "TODO"}}),
    )
    created = (_tc_event("r", base),)
    data = TimelineData(updated, tasks, members_by_id={WM_VOVA: "V"},
                        created_events=created)
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts)
    row = next((r for r in dto.rows if r.user_id == WM_VOVA), None)
    # Vova has no completed (task reverted). pending=0: задача снова TODO,
    # а «Активных» считает только V_RABOTE.
    assert row is not None
    assert row.completed == 0
    assert row.pending_count == 0
    assert dto.totals is not None
    assert dto.totals.completed == 0


def test_deleted_task_leaves_no_ghost_metrics() -> None:
    """Task deleted in UI → timelineActivity events remain, but /rest/tasks
    stops returning it. compute_report must ignore leftover events so the
    operator doesn't show "0 completed but avg_response_time = 1h32m".
    """
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)
    base = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)

    # tasks list is EMPTY (ghost task was deleted)
    tasks: tuple[dict, ...] = ()
    updated = (
        _tu_event("ghost", base, {"assigneeId": {"before": None, "after": WM_VOVA}}),
        _tu_event("ghost", base + timedelta(hours=2),
                  {"status": {"before": "TODO", "after": "VYPOLNENO"}}),
    )
    created = (_tc_event("ghost", base - timedelta(minutes=5)),)
    data = TimelineData(updated, tasks, members_by_id={WM_VOVA: "V"},
                        created_events=created)
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts)
    # No row for WM_VOVA at all (no pending / no completion / no response)
    assert not any(r.user_id == WM_VOVA for r in dto.rows)


def test_outgoing_callback_tasks_excluded_from_metrics() -> None:
    """Tasks flagged isOutgoingCallback=True are historical mush created
    when poller treated every OUTGOING звонок как новый тикет. Reports
    must ignore them — counts, durations, response time, everything."""
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)
    base = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)

    tasks = (
        {"id": "real",     "createdAt": _iso(base),
         "assigneeId": WM_VOVA, "status": "VYPOLNENO"},
        {"id": "callback", "createdAt": _iso(base),
         "assigneeId": WM_VOVA, "status": "VYPOLNENO",
         "isOutgoingCallback": True},
    )
    updated = (
        _tu_event("real", base, {"assigneeId": {"before": None, "after": WM_VOVA}}),
        _tu_event("real", base + timedelta(hours=1),
                  {"status": {"before": "TODO", "after": "VYPOLNENO"}}),
        _tu_event("callback", base, {"assigneeId": {"before": None, "after": WM_VOVA}}),
        _tu_event("callback", base + timedelta(hours=1),
                  {"status": {"before": "TODO", "after": "VYPOLNENO"}}),
    )
    created = (
        _tc_event("real", base - timedelta(minutes=5)),
        _tc_event("callback", base - timedelta(minutes=5)),
    )
    data = TimelineData(updated, tasks, members_by_id={WM_VOVA: "V"},
                        created_events=created)
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts)
    # Only `real` should count
    rows = [r for r in dto.rows if r.user_id == WM_VOVA]
    assert len(rows) == 1
    assert rows[0].completed == 1, "callback task must not count as completed"


def test_created_breakdown_sums_to_total() -> None:
    """Новые+Вработе+Выполнено+Приостановлено+Вкорзине+Прочее == Создано."""
    from_ts = datetime(2026, 4, 1, tzinfo=UTC)
    to_ts = datetime(2026, 4, 30, tzinfo=UTC)
    c = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    tasks = (
        {"id": "s1", "createdAt": _iso(c), "assigneeId": WM_VOVA, "status": "TODO"},
        {"id": "s2", "createdAt": _iso(c), "assigneeId": WM_VOVA, "status": "V_RABOTE"},
        {"id": "s3", "createdAt": _iso(c), "assigneeId": WM_VOVA, "status": "VYPOLNENO"},
        {"id": "s3b", "createdAt": _iso(c), "assigneeId": WM_VOVA, "status": "DONE"},
        {"id": "s4", "createdAt": _iso(c), "assigneeId": WM_VOVA, "status": "KORZINA"},
        {"id": "s5", "createdAt": _iso(c), "assigneeId": WM_VOVA, "status": "PRIOSTANOVLENO"},
    )
    data = TimelineData((), tasks, members_by_id={WM_VOVA: "V"})
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    assert dto.total_created_in_period == 6
    assert dto.created_new == 1
    assert dto.created_in_progress == 1
    assert dto.created_completed == 2   # VYPOLNENO + DONE
    assert dto.created_paused == 1      # PRIOSTANOVLENO
    assert dto.created_trashed == 1     # KORZINA
    assert dto.created_other == 0
    assert (dto.created_new + dto.created_in_progress + dto.created_completed
            + dto.created_paused + dto.created_trashed
            + dto.created_other) == dto.total_created_in_period


def test_created_window_uses_task_created_event_not_column() -> None:
    """«Создано в периоде» по реальному task.created, а не по backfill-колонке.

    Колонка createdAt = время ATS-звонка (вне окна), а реальная вставка в
    Twenty (task.created) — внутри окна. Задача должна попасть в период.
    """
    from_ts = datetime(2026, 5, 1, tzinfo=UTC)
    to_ts = datetime(2026, 5, 31, tzinfo=UTC)
    col_outside = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)   # backfilled ATS time — вне окна
    insert_inside = datetime(2026, 5, 2, 9, 0, tzinfo=UTC)   # реальная вставка — в окне
    tasks = ({"id": "drift", "createdAt": _iso(col_outside),
              "assigneeId": WM_VOVA, "status": "TODO"},)
    created = (_tc_event("drift", insert_inside),)
    data = TimelineData((), tasks, members_by_id={WM_VOVA: "V"}, created_events=created)
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    assert dto.total_created_in_period == 1   # посчитана по task.created, не по колонке
    assert dto.created_new == 1


def test_work_duration_clipped_to_period() -> None:
    """«Время в работе» обрезается по периоду — за неделю не может быть 13 дней.

    Задача в «В работе» с 20.04 (до окна), закрыта 03.05 (в окне).
    Окно — весь май. Считается только май-часть: 01.05→03.05 = 2 дня.
    """
    from_ts = datetime(2026, 5, 1, tzinfo=UTC)
    to_ts = datetime(2026, 5, 31, tzinfo=UTC)
    in_work = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)   # до окна
    done = datetime(2026, 5, 3, 0, 0, tzinfo=UTC)        # в окне (01.05→03.05 = 2д)
    tasks = ({"id": "clip", "createdAt": _iso(in_work),
              "assigneeId": WM_VOVA, "status": "VYPOLNENO"},)
    events = (
        _tu_event("clip", in_work, {"status": {"before": "TODO", "after": "V_RABOTE"}}, wmid=WM_VOVA),
        _tu_event("clip", done, {"status": {"before": "V_RABOTE", "after": "VYPOLNENO"}}, wmid=WM_VOVA),
    )
    data = TimelineData(events, tasks, members_by_id={WM_VOVA: "V"})
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    row = next(r for r in dto.rows if r.user_id == WM_VOVA)
    assert row.total_duration_seconds == 2 * 86400   # только 01.05→03.05, не 13 дней


def test_response_time_only_for_tasks_created_in_window() -> None:
    """«Ср.реаг» — только по задачам, созданным в периоде.

    Старый бэклог (создан до окна, назначен в окне) НЕ должен давать
    выброс «31 день». Считается только свежий поток.
    """
    from_ts = datetime(2026, 5, 1, tzinfo=UTC)
    to_ts = datetime(2026, 5, 31, tzinfo=UTC)
    tasks = (
        {"id": "old", "createdAt": _iso(datetime(2026, 4, 15, tzinfo=UTC)),
         "assigneeId": WM_VOVA, "status": "TODO"},
        {"id": "fresh", "createdAt": _iso(datetime(2026, 5, 2, tzinfo=UTC)),
         "assigneeId": WM_VOVA, "status": "TODO"},
    )
    created = (
        _tc_event("old", datetime(2026, 4, 15, 9, 0, tzinfo=UTC)),     # до окна
        _tc_event("fresh", datetime(2026, 5, 2, 9, 0, tzinfo=UTC)),    # в окне
    )
    updated = (
        # old: назначен в окне (но создан до окна) → НЕ в реакции
        _tu_event("old", datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
                  {"assigneeId": {"before": None, "after": WM_VOVA}}),
        # fresh: назначен через 1ч после создания → реакция 1ч
        _tu_event("fresh", datetime(2026, 5, 2, 10, 0, tzinfo=UTC),
                  {"assigneeId": {"before": None, "after": WM_VOVA}}),
    )
    data = TimelineData(updated, tasks, members_by_id={WM_VOVA: "V"}, created_events=created)
    dto = compute_report(data, from_ts=from_ts, to_ts=to_ts, scope=ReportScope.OVERALL)
    row = next(r for r in dto.rows if r.user_id == WM_VOVA)
    assert row.avg_response_time_seconds == 3600   # только fresh (1ч), old (20 дней) исключён
