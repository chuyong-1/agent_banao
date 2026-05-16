"""
test_bench_health.py — Bench + Workforce Health Test Suite  (v1)
================================================================
Tests for the v3 additive extensions: Bench Availability Engine and
Workforce / Workload Health Engine.

Coverage:
  • Bench status classification (all 5 categories)
  • Bench percentage / projected bench date accuracy
  • BenchMetrics serialisation (model_dump round-trip)
  • WorkforceHealthMetrics scoring formulas
  • Utilization band classification (all 5 bands)
  • Burnout risk label classification (all 4 labels)
  • Health warnings accuracy
  • Health-aware fit_score when HEALTH_AWARE_SCORING enabled
  • compute_bench_summary org aggregation
  • compute_health_summary org aggregation
  • Edge cases: zero-capacity, overallocated, all-overdue, fully benched
  • Regression: existing pipeline tests unaffected (spot-checks)
  • Stress: 1 000-employee synthetic dataset — determinism + no exceptions

Run:
    cd resource_planner
    python3 test_bench_health.py
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(__file__))
logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# ── Imports under test ─────────────────────────────────────────────────────────
from bench import compute_bench_metrics, compute_bench_summary, BENCH_THRESHOLD_HOURS
from health import (
    compute_workforce_health,
    compute_health_summary,
    health_aware_fit_score,
    _utilization_band,
    _burnout_risk_label,
)
from state import (
    BenchStatus, BurnoutRisk, UtilizationBand,
    BenchMetrics, WorkforceHealthMetrics,
)
from nodes import _today, _parse_date

# ── Helpers ────────────────────────────────────────────────────────────────────
TODAY: date = _today()
G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; B = "\033[1m"; X = "\033[0m"
_RESULTS: List[Tuple[str, bool, str]] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    _RESULTS.append((label, passed, detail))
    icon = f"{G}PASS{X}" if passed else f"{R}FAIL{X}"
    extra = f"  → {detail}" if detail and not passed else ""
    print(f"  [{icon}]  {label}{extra}")


# ── Fixture factories ──────────────────────────────────────────────────────────

def _make_employee(
    eid: str = "E1",
    capacity: float = 40.0,
    skills: List[Dict] | None = None,
) -> Dict[str, Any]:
    return {
        "id":   eid,
        "name": f"Employee {eid}",
        "role": "Engineer",
        "department": "Engineering",
        "capacity_hours_per_week": capacity,
        "skills": skills or [{"name": "Python", "proficiency": 4}],
        "hourly_rate_usd": 80.0,
    }


def _asgn(eid: str, end_date: date, hours: float = 20.0) -> Dict[str, Any]:
    start = TODAY - timedelta(weeks=4)
    return {
        "assignment_id": f"A_{eid}_{end_date}",
        "employee_id": eid,
        "project_name": "Test Project",
        "hours_per_week": hours,
        "start_date": start.isoformat(),
        "end_date": end_date.isoformat(),
    }


def _metric_dict(
    util_pct: float = 50.0,
    available: float = 20.0,
    capacity: float = 40.0,
    overalloc: bool = False,
    overdue: int = 0,
    eff_overdue: int = 0,
    in_progress: int = 0,
    leave_pct: float = 0.0,
    reliability: float = 80.0,
) -> Dict[str, Any]:
    """Build a minimal metrics dict as produced by compute_metrics_node."""
    return {
        "employee_id":           "E1",
        "name":                  "Test Employee",
        "role":                  "Engineer",
        "capacity_hours_per_week": capacity,
        "assigned_hours_per_week": capacity - available,
        "active_bounty_hours_weekly": 0.0,
        "effective_assigned_hours":   capacity - available,
        "available_hours_per_week":   available,
        "utilization_pct":            util_pct,
        "availability_score":         max(0.0, 100.0 - util_pct),
        "projected_free_date":        TODAY.isoformat(),
        "overallocation_flag":        overalloc,
        "is_disqualified":            False,
        "disqualification_reason":    None,
        "leave_detail": {
            "has_any_overlap":    leave_pct > 0,
            "overlap_days":       int(leave_pct * 0.56),
            "project_total_days": 56,
            "overlap_pct":        leave_pct,
            "leave_periods":      [],
            "is_fully_blocked":   leave_pct >= 100,
            "is_mostly_blocked":  leave_pct >= 50,
        },
        "bounty_metrics": {
            "total_assigned":       overdue + eff_overdue + in_progress,
            "completed":            0,
            "in_progress":          in_progress,
            "not_started":          0,
            "overdue":              overdue,
            "effectively_overdue":  eff_overdue,
            "total_problematic":    overdue + eff_overdue,
            "active_bounty_hours":  in_progress * 8.0,
            "active_bounty_hours_weekly": in_progress * 2.0,
            "reliability_score":    reliability,
            "overdue_titles":       [f"Task{i}" for i in range(overdue)],
            "in_progress_titles":   [f"IP{i}" for i in range(in_progress)],
            "completed_titles":     [],
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Bench Status Classification
# ═══════════════════════════════════════════════════════════════════════════════

def section_bench_status() -> None:
    print(f"\n{B}── Section 1: Bench Status Classification ──{X}")

    emp = _make_employee()

    # OVERALLOCATED
    bm = compute_bench_metrics(emp, [], 0.0, 110.0, TODAY)
    check("BENCH: util > 100 → OVERALLOCATED",
          bm.bench_status == BenchStatus.OVERALLOCATED,
          f"got {bm.bench_status}")

    # AVAILABLE_NOW — no assignments
    bm = compute_bench_metrics(emp, [], 40.0, 0.0, TODAY)
    check("BENCH: no assignments → AVAILABLE_NOW",
          bm.bench_status == BenchStatus.AVAILABLE_NOW,
          f"got {bm.bench_status}")

    # ROLLING_OFF_SOON — assignment ends within ROLLING_OFF_WEEKS
    soon_end = TODAY + timedelta(weeks=1)
    asgn = [_asgn("E1", soon_end, hours=20.0)]
    bm = compute_bench_metrics(emp, asgn, 20.0, 50.0, TODAY)
    check("BENCH: all assignments ending soon → ROLLING_OFF_SOON",
          bm.bench_status == BenchStatus.ROLLING_OFF_SOON,
          f"got {bm.bench_status}")

    # PARTIALLY_ALLOCATED — free capacity >= threshold, not rolling off
    far_end = TODAY + timedelta(weeks=12)
    asgn = [_asgn("E1", far_end, hours=20.0)]
    bm = compute_bench_metrics(emp, asgn, 20.0, 50.0, TODAY)
    check("BENCH: avail >= threshold, not rolling off → PARTIALLY_ALLOCATED",
          bm.bench_status == BenchStatus.PARTIALLY_ALLOCATED,
          f"got {bm.bench_status}")

    # FULLY_ALLOCATED — free capacity < threshold
    asgn_heavy = [_asgn("E1", far_end, hours=35.0)]
    bm = compute_bench_metrics(emp, asgn_heavy, 5.0, 87.5, TODAY)
    check("BENCH: avail < threshold, not rolling off → FULLY_ALLOCATED",
          bm.bench_status == BenchStatus.FULLY_ALLOCATED,
          f"got {bm.bench_status}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Bench Metrics Accuracy
# ═══════════════════════════════════════════════════════════════════════════════

def section_bench_accuracy() -> None:
    print(f"\n{B}── Section 2: Bench Metrics Accuracy ──{X}")

    emp = _make_employee(capacity=40.0)

    # bench_percentage for AVAILABLE_NOW should be 100%
    bm = compute_bench_metrics(emp, [], 40.0, 0.0, TODAY)
    check("BENCH: fully benched → bench_percentage == 100",
          bm.bench_percentage == 100.0,
          f"got {bm.bench_percentage}")

    # bench_percentage for 50% utilized
    far = TODAY + timedelta(weeks=12)
    bm = compute_bench_metrics(emp, [_asgn("E1", far, 20.0)], 20.0, 50.0, TODAY)
    check("BENCH: 50% utilized → bench_percentage == 50",
          bm.bench_percentage == 50.0,
          f"got {bm.bench_percentage}")

    # projected_bench_date = day after last assignment end for ROLLING_OFF_SOON
    soon_end = TODAY + timedelta(days=10)
    bm = compute_bench_metrics(emp, [_asgn("E1", soon_end, 20.0)], 20.0, 50.0, TODAY)
    expected_date = (soon_end + timedelta(days=1)).isoformat()
    check("BENCH: projected_bench_date = day after last assignment",
          bm.projected_bench_date == expected_date,
          f"expected {expected_date} got {bm.projected_bench_date}")

    # bench_duration_days for AVAILABLE_NOW → 0
    bm = compute_bench_metrics(emp, [], 40.0, 0.0, TODAY)
    check("BENCH: AVAILABLE_NOW → bench_duration_days == 0",
          bm.bench_duration_days == 0,
          f"got {bm.bench_duration_days}")

    # bench_duration_days > 0 for future end
    future_end = TODAY + timedelta(days=20)
    bm = compute_bench_metrics(emp, [_asgn("E1", future_end, 20.0)], 20.0, 50.0, TODAY)
    check("BENCH: ROLLING_OFF_SOON → bench_duration_days == days until end",
          bm.bench_duration_days == 20,
          f"got {bm.bench_duration_days}")

    # primary_skill_cluster — top skill first
    skilled_emp = _make_employee(skills=[
        {"name": "Python", "proficiency": 5},
        {"name": "Django", "proficiency": 3},
        {"name": "AWS",    "proficiency": 4},
    ])
    bm = compute_bench_metrics(skilled_emp, [], 40.0, 0.0, TODAY)
    check("BENCH: primary_skill_cluster top = highest proficiency",
          bm.primary_skill_cluster[0] == "Python",
          f"got {bm.primary_skill_cluster}")

    # last_project_end_date populated from active assignments
    far2 = TODAY + timedelta(weeks=8)
    bm = compute_bench_metrics(emp, [_asgn("E1", far2, 20.0)], 20.0, 50.0, TODAY)
    check("BENCH: last_project_end_date matches latest assignment end",
          bm.last_project_end_date == far2.isoformat(),
          f"got {bm.last_project_end_date}")

    # last_project_end_date is None when no active assignments
    bm = compute_bench_metrics(emp, [], 40.0, 0.0, TODAY)
    check("BENCH: no assignments → last_project_end_date is None",
          bm.last_project_end_date is None,
          "")

    # Model serialises without error
    try:
        bm.model_dump()
        check("BENCH: model_dump() succeeds without error", True)
    except Exception as exc:
        check("BENCH: model_dump() succeeds without error", False, str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Utilization Band
# ═══════════════════════════════════════════════════════════════════════════════

def section_utilization_band() -> None:
    print(f"\n{B}── Section 3: Utilization Band ──{X}")

    cases = [
        (0.0,   UtilizationBand.UNDERUTILIZED),
        (25.0,  UtilizationBand.UNDERUTILIZED),
        (25.1,  UtilizationBand.HEALTHY),
        (75.0,  UtilizationBand.HEALTHY),
        (75.1,  UtilizationBand.HIGH_UTILIZATION),
        (90.0,  UtilizationBand.HIGH_UTILIZATION),
        (90.1,  UtilizationBand.OVERLOADED),
        (100.0, UtilizationBand.OVERLOADED),
        (100.1, UtilizationBand.CRITICAL),
        (150.0, UtilizationBand.CRITICAL),
    ]
    for pct, expected in cases:
        got = _utilization_band(pct)
        check(f"BAND: {pct}% → {expected.value}",
              got == expected,
              f"got {got}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Burnout Risk Label
# ═══════════════════════════════════════════════════════════════════════════════

def section_burnout_label() -> None:
    print(f"\n{B}── Section 4: Burnout Risk Label ──{X}")

    cases = [
        (0.0,  BurnoutRisk.LOW),
        (30.0, BurnoutRisk.LOW),
        (30.1, BurnoutRisk.MODERATE),
        (60.0, BurnoutRisk.MODERATE),
        (60.1, BurnoutRisk.HIGH),
        (80.0, BurnoutRisk.HIGH),
        (80.1, BurnoutRisk.CRITICAL),
        (100.0, BurnoutRisk.CRITICAL),
    ]
    for score, expected in cases:
        got = _burnout_risk_label(score)
        check(f"BURNOUT LABEL: {score} → {expected.value}",
              got == expected,
              f"got {got}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Workload & Burnout Score Formulas
# ═══════════════════════════════════════════════════════════════════════════════

def section_health_scores() -> None:
    print(f"\n{B}── Section 5: Health Score Formulas ──{X}")

    # Healthy employee — 50% util, no issues
    m = _metric_dict(util_pct=50.0, available=20.0)
    wh = compute_workforce_health(m, TODAY)
    check("HEALTH: 50% util, no issues → workload_score == 50",
          wh.workload_score == 50.0,
          f"got {wh.workload_score}")
    check("HEALTH: 50% util → burnout_risk_score == 50",
          wh.burnout_risk_score == 50.0,
          f"got {wh.burnout_risk_score}")

    # Overdue bounties reduce workload and increase burnout
    m_bad = _metric_dict(util_pct=80.0, available=8.0, overdue=2)
    wh_bad = compute_workforce_health(m_bad, TODAY)
    check("HEALTH: 2 overdue bounties reduce workload by 10",
          wh_bad.workload_score == max(0.0, 100.0 - 80.0 - 2 * 5.0),
          f"expected {max(0.0, 100.0 - 80.0 - 2*5.0)} got {wh_bad.workload_score}")
    check("HEALTH: 2 overdue bounties add 20 to burnout",
          wh_bad.burnout_risk_score == min(100.0, 80.0 + 2 * 10.0),
          f"expected {min(100.0, 80.0 + 2*10.0)} got {wh_bad.burnout_risk_score}")

    # Overallocation penalty
    m_overalloc = _metric_dict(util_pct=110.0, available=0.0, overalloc=True)
    wh_overalloc = compute_workforce_health(m_overalloc, TODAY)
    check("HEALTH: overallocated → workload_score = 0 (clamped)",
          wh_overalloc.workload_score == 0.0,
          f"got {wh_overalloc.workload_score}")
    check("HEALTH: overallocated → burnout increased by overalloc penalty",
          wh_overalloc.burnout_risk_score >= 100.0,
          f"got {wh_overalloc.burnout_risk_score}")

    # Zero util → 100 workload, low burnout
    m_zero = _metric_dict(util_pct=0.0, available=40.0)
    wh_zero = compute_workforce_health(m_zero, TODAY)
    check("HEALTH: 0% util → workload_score == 100",
          wh_zero.workload_score == 100.0,
          f"got {wh_zero.workload_score}")
    check("HEALTH: 0% util → burnout_risk LOW",
          wh_zero.burnout_risk == BurnoutRisk.LOW,
          f"got {wh_zero.burnout_risk}")

    # Sustainability = workload*0.6 + (100−burnout)*0.4
    expected_sust = round(wh_zero.workload_score * 0.6 + (100 - wh_zero.burnout_risk_score) * 0.4, 1)
    check("HEALTH: sustainability_score formula correct",
          wh_zero.sustainability_score == expected_sust,
          f"expected {expected_sust} got {wh_zero.sustainability_score}")

    # Scores always in [0, 100]
    for util in [0, 50, 100, 150]:
        for od in [0, 5]:
            mi = _metric_dict(util_pct=float(util), available=max(0.0, 40.0 - util/100.0*40), overdue=od)
            whi = compute_workforce_health(mi, TODAY)
            check(f"HEALTH: scores in [0,100] for util={util} overdue={od}",
                  0 <= whi.workload_score <= 100 and
                  0 <= whi.burnout_risk_score <= 100 and
                  0 <= whi.sustainability_score <= 100,
                  f"workload={whi.workload_score} burnout={whi.burnout_risk_score} sust={whi.sustainability_score}")

    # model_dump works
    try:
        m = _metric_dict()
        wh = compute_workforce_health(m, TODAY)
        wh.model_dump()
        check("HEALTH: model_dump() succeeds", True)
    except Exception as exc:
        check("HEALTH: model_dump() succeeds", False, str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — Health Warnings
# ═══════════════════════════════════════════════════════════════════════════════

def section_health_warnings() -> None:
    print(f"\n{B}── Section 6: Health Warnings ──{X}")

    # High burnout → warning
    m = _metric_dict(util_pct=90.0, available=4.0, overdue=3)
    wh = compute_workforce_health(m, TODAY)
    has_burnout_warn = any("burnout" in w.lower() or "High burnout" in w for w in wh.health_warnings)
    check("WARN: high burnout risk triggers 'High burnout probability'",
          has_burnout_warn,
          f"got {wh.health_warnings}")

    # Overallocated warning
    m = _metric_dict(util_pct=110.0, available=0.0, overalloc=True)
    wh = compute_workforce_health(m, TODAY)
    check("WARN: overallocated → overallocation warning",
          any("overallocation" in w.lower() for w in wh.health_warnings),
          f"got {wh.health_warnings}")

    # Underutilized warning
    m = _metric_dict(util_pct=10.0, available=36.0)
    wh = compute_workforce_health(m, TODAY)
    check("WARN: underutilized → bench underutilization warning",
          any("underutiliz" in w.lower() or "bench" in w.lower() for w in wh.health_warnings),
          f"got {wh.health_warnings}")

    # Multiple concurrent bounties warning
    m = _metric_dict(util_pct=70.0, available=12.0, in_progress=3)
    wh = compute_workforce_health(m, TODAY)
    check("WARN: 3 in-progress bounties → context-switching warning",
          any("concurrent" in w.lower() or "context" in w.lower() for w in wh.health_warnings),
          f"got {wh.health_warnings}")

    # No warnings for healthy employee
    m = _metric_dict(util_pct=60.0, available=16.0, reliability=90.0)
    wh = compute_workforce_health(m, TODAY)
    check("WARN: healthy employee → no warnings",
          len(wh.health_warnings) == 0,
          f"got {wh.health_warnings}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — Health-Aware Fit Score
# ═══════════════════════════════════════════════════════════════════════════════

def section_health_aware_score() -> None:
    print(f"\n{B}── Section 7: Health-Aware Fit Score ──{X}")

    from health import HEALTH_W_AVAILABILITY, HEALTH_W_SKILL, HEALTH_W_RELIABILITY, HEALTH_W_HEALTH

    # Exact formula check
    avail, skill, rely, sust = 80.0, 70.0, 90.0, 60.0
    expected = round(
        HEALTH_W_AVAILABILITY * avail + HEALTH_W_SKILL * skill +
        HEALTH_W_RELIABILITY  * rely  + HEALTH_W_HEALTH * sust, 1
    )
    got = health_aware_fit_score(avail, skill, rely, sust)
    check("H-AWARE: formula matches expected",
          got == expected,
          f"expected {expected} got {got}")

    # Result always in [0, 100]
    for a, s, r, h in [(100, 100, 100, 100), (0, 0, 0, 0), (50, 50, 50, 50)]:
        score = health_aware_fit_score(a, s, r, h)
        check(f"H-AWARE: score in [0,100] for ({a},{s},{r},{h})",
              0.0 <= score <= 100.0, f"got {score}")

    # Weights sum validation (warn if not ≈1 — misconfiguration guard)
    weight_sum = round(HEALTH_W_AVAILABILITY + HEALTH_W_SKILL + HEALTH_W_RELIABILITY + HEALTH_W_HEALTH, 4)
    check("H-AWARE: weights sum to 1.0",
          abs(weight_sum - 1.0) < 0.01,
          f"sum={weight_sum}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — Org-Level Summaries
# ═══════════════════════════════════════════════════════════════════════════════

def section_org_summaries() -> None:
    print(f"\n{B}── Section 8: Org-Level Summaries ──{X}")

    emp = _make_employee(capacity=40.0)

    # Build two bench entries: one available, one fully allocated
    bm_avail = compute_bench_metrics(emp, [], 40.0, 0.0, TODAY)
    bm_full  = compute_bench_metrics(emp, [_asgn("E1", TODAY + timedelta(weeks=12), 38.0)],
                                     2.0, 95.0, TODAY)

    all_metrics = [
        {"employee_id": "E1", "name": "Alice",
         "role": "Dev", "available_hours_per_week": 40.0,
         "bench_metrics": bm_avail.model_dump(),
         "workforce_health": compute_workforce_health(_metric_dict(util_pct=0.0, available=40.0), TODAY).model_dump()},
        {"employee_id": "E2", "name": "Bob",
         "role": "Dev", "available_hours_per_week": 2.0,
         "bench_metrics": bm_full.model_dump(),
         "workforce_health": compute_workforce_health(_metric_dict(util_pct=95.0, available=2.0), TODAY).model_dump()},
    ]

    bench_summ = compute_bench_summary(all_metrics)
    check("ORG: bench available_now_count == 1",
          bench_summ["available_now_count"] == 1, f"got {bench_summ}")
    check("ORG: bench total_bench_capacity >= 40",
          bench_summ["total_bench_capacity_hours_per_week"] >= 40.0,
          f"got {bench_summ['total_bench_capacity_hours_per_week']}")
    check("ORG: bench fully_allocated_count == 1",
          bench_summ["fully_allocated_count"] == 1, f"got {bench_summ}")

    health_summ = compute_health_summary(all_metrics)
    check("ORG: health total_employees_evaluated == 2",
          health_summ["total_employees_evaluated"] == 2, "")
    check("ORG: health overloaded count correct",
          health_summ["overloaded_employee_count"] >= 1,
          f"got {health_summ['overloaded_employee_count']}")
    check("ORG: health summary has average_utilization_pct",
          "average_utilization_pct" in health_summ, "")

    # Edge: empty list
    check("ORG: bench_summary of empty list doesn't crash",
          compute_bench_summary([]) == {
              "total_bench_capacity_hours_per_week": 0.0,
              "available_now_count": 0, "partially_allocated_count": 0,
              "rolling_off_soon_count": 0, "fully_allocated_count": 0,
              "overallocated_count": 0,
              "bench_threshold_hours": BENCH_THRESHOLD_HOURS,
              "rolling_off_weeks": 2,
              "available_now": [], "partially_allocated": [], "rolling_off_soon": [],
          }, "")
    check("ORG: health_summary of empty list returns empty dict",
          compute_health_summary([]) == {}, "")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — Edge Cases
# ═══════════════════════════════════════════════════════════════════════════════

def section_edge_cases() -> None:
    print(f"\n{B}── Section 9: Edge Cases ──{X}")

    emp_zero = _make_employee(capacity=0.0)

    # Zero capacity employee
    bm = compute_bench_metrics(emp_zero, [], 0.0, 0.0, TODAY)
    check("EDGE: zero capacity → AVAILABLE_NOW (not OVERALLOCATED)",
          bm.bench_status == BenchStatus.AVAILABLE_NOW,
          f"got {bm.bench_status}")
    check("EDGE: zero capacity → bench_percentage == 0",
          bm.bench_percentage == 0.0,
          f"got {bm.bench_percentage}")

    # All overdue bounties — burnout maxed
    m_all_od = _metric_dict(util_pct=80.0, available=8.0, overdue=5)
    wh = compute_workforce_health(m_all_od, TODAY)
    check("EDGE: 5 overdue bounties → burnout_risk_score clamped at 100",
          wh.burnout_risk_score <= 100.0,
          f"got {wh.burnout_risk_score}")

    # Workload never negative
    m_extreme = _metric_dict(util_pct=110.0, available=0.0, overalloc=True, overdue=10)
    wh = compute_workforce_health(m_extreme, TODAY)
    check("EDGE: extreme overload → workload_score >= 0",
          wh.workload_score >= 0.0,
          f"got {wh.workload_score}")

    # bench_percentage never exceeds 100
    emp_big = _make_employee(capacity=40.0)
    bm = compute_bench_metrics(emp_big, [], 45.0, 0.0, TODAY)   # avail > capacity (data anomaly)
    check("EDGE: avail > capacity → bench_percentage clamped at 100",
          bm.bench_percentage <= 100.0,
          f"got {bm.bench_percentage}")

    # Multiple assignments — rolling off uses latest end
    far_end1 = TODAY + timedelta(days=5)
    far_end2 = TODAY + timedelta(days=12)
    emp2 = _make_employee(capacity=40.0)
    asgns = [_asgn("E1", far_end1, 10.0), _asgn("E1", far_end2, 10.0)]
    bm = compute_bench_metrics(emp2, asgns, 20.0, 50.0, TODAY)
    expected_bench_date = (far_end2 + timedelta(days=1)).isoformat()
    check("EDGE: multiple assignments → projected_bench_date = max end + 1",
          bm.projected_bench_date == expected_bench_date,
          f"expected {expected_bench_date} got {bm.projected_bench_date}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — Pipeline Regression (existing tests unbroken)
# ═══════════════════════════════════════════════════════════════════════════════

def section_pipeline_regression() -> None:
    print(f"\n{B}── Section 10: Pipeline Regression (spot-checks) ──{X}")

    from graph import app
    from server import _build_ui_payload

    def _invoke(desc: str) -> Dict:
        state = {
            "raw_project_input": desc,
            "extracted_requirements": None, "raw_erp_data": None,
            "processed_metrics": None, "ranked_candidates": None,
            "disqualified_candidates": None,
            "bench_summary": None, "workforce_health_summary": None,
            "errors": [],
        }
        return _build_ui_payload(app.invoke(state))

    p = _invoke("Need Python and Kafka engineers for 20 h/wk for 8 weeks.")
    candidates   = p.get("candidates", [])
    disqualified = p.get("disqualified", [])

    check("REGRESSION: pipeline produces candidates",
          len(candidates) > 0, f"got {len(candidates)}")
    check("REGRESSION: fit scores in [0, 100]",
          all(0 <= c["scores"]["fit_score"] <= 100 for c in candidates), "")
    check("REGRESSION: ranking is descending by fit_score",
          [c["scores"]["fit_score"] for c in candidates] ==
          sorted([c["scores"]["fit_score"] for c in candidates], reverse=True), "")
    check("REGRESSION: no employee in both ranked and disqualified",
          {c["employee_id"] for c in candidates}.isdisjoint(
              {d["employee_id"] for d in disqualified}), "")

    # NEW v3 fields present in payload
    check("REGRESSION: payload contains bench_summary key",
          "bench_summary" in p, "")
    check("REGRESSION: payload contains workforce_health_summary key",
          "workforce_health_summary" in p, "")

    # Existing summary shape unchanged
    s = p["summary"]
    for key in ("total_evaluated", "candidates_eligible", "candidates_disqualified",
                "top_fit_score", "recommendation", "scoring_weights"):
        check(f"REGRESSION: summary.{key} present",
              key in s, f"keys={list(s.keys())}")

    # bench_summary shape
    bs = p.get("bench_summary") or {}
    if bs:
        for key in ("total_bench_capacity_hours_per_week", "available_now_count",
                    "partially_allocated_count", "rolling_off_soon_count"):
            check(f"REGRESSION: bench_summary.{key} present",
                  key in bs, f"keys={list(bs.keys())}")

    # workforce_health_summary shape
    whs = p.get("workforce_health_summary") or {}
    if whs:
        for key in ("total_employees_evaluated", "average_utilization_pct",
                    "burnout_risk_count", "burnout_alerts"):
            check(f"REGRESSION: workforce_health_summary.{key} present",
                  key in whs, f"keys={list(whs.keys())}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — Stress Test: 1 000 synthetic employees
# ═══════════════════════════════════════════════════════════════════════════════

def section_stress() -> None:
    print(f"\n{B}── Section 11: Stress Test (1 000 employees) ──{X}")

    import random
    rng = random.Random(42)

    metrics_1k: List[Dict[str, Any]] = []
    exceptions: List[str] = []

    for i in range(1000):
        util = rng.uniform(0, 130)
        avail = max(0.0, 40.0 - util / 100.0 * 40.0)
        overalloc = util > 100
        overdue = rng.randint(0, 4)
        eff_od  = rng.randint(0, 2)
        in_prog = rng.randint(0, 5)
        leave_pct = rng.uniform(0, 100)
        reliability = rng.uniform(0, 100)

        m = _metric_dict(
            util_pct=round(util, 1), available=round(avail, 1),
            overalloc=overalloc, overdue=overdue,
            eff_overdue=eff_od, in_progress=in_prog,
            leave_pct=round(leave_pct, 1), reliability=round(reliability, 1),
        )
        m["employee_id"] = f"E{i}"
        m["name"] = f"Employee {i}"

        emp = _make_employee(f"E{i}", capacity=40.0)
        far_end = TODAY + timedelta(days=rng.randint(0, 90))
        asgns = [] if util < 5 else [_asgn(f"E{i}", far_end, min(40, util / 100.0 * 40))]

        try:
            bm = compute_bench_metrics(emp, asgns, avail, util, TODAY)
            wh = compute_workforce_health(m, TODAY)
            m["bench_metrics"]    = bm.model_dump()
            m["workforce_health"] = wh.model_dump()
            metrics_1k.append(m)
        except Exception as exc:
            exceptions.append(f"E{i}: {exc}")

    check("STRESS: no exceptions on 1 000 synthetic employees",
          len(exceptions) == 0,
          f"exceptions: {exceptions[:3]}")

    check("STRESS: all 1 000 employees processed",
          len(metrics_1k) == 1000,
          f"got {len(metrics_1k)}")

    all_workload = [m["workforce_health"]["workload_score"] for m in metrics_1k]
    check("STRESS: all workload_scores in [0, 100]",
          all(0.0 <= s <= 100.0 for s in all_workload), "")

    all_burnout = [m["workforce_health"]["burnout_risk_score"] for m in metrics_1k]
    check("STRESS: all burnout_scores in [0, 100]",
          all(0.0 <= s <= 100.0 for s in all_burnout), "")

    all_bench_pct = [m["bench_metrics"]["bench_percentage"] for m in metrics_1k]
    check("STRESS: all bench_percentages in [0, 100]",
          all(0.0 <= p <= 100.0 for p in all_bench_pct), "")

    # Determinism — run again with same seed and compare
    rng2 = random.Random(42)
    m0_util = rng2.uniform(0, 130)
    m0_avail = max(0.0, 40.0 - m0_util / 100.0 * 40.0)
    m0 = _metric_dict(util_pct=round(m0_util, 1), available=round(m0_avail, 1))
    wh0 = compute_workforce_health(m0, TODAY)
    wh0b = compute_workforce_health(m0, TODAY)
    check("STRESS: compute_workforce_health is deterministic",
          wh0.workload_score == wh0b.workload_score and
          wh0.burnout_risk_score == wh0b.burnout_risk_score, "")

    # Org summaries on 1 000 employees
    try:
        bs  = compute_bench_summary(metrics_1k)
        whs = compute_health_summary(metrics_1k)
        check("STRESS: compute_bench_summary on 1 000 employees succeeds",
              "total_bench_capacity_hours_per_week" in bs, "")
        check("STRESS: compute_health_summary on 1 000 employees succeeds",
              whs.get("total_employees_evaluated") == 1000, "")
    except Exception as exc:
        check("STRESS: org summaries on 1 000 employees succeed", False, str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"\n{B}{'═'*70}{X}")
    print(f"{B}  BENCH + WORKFORCE HEALTH TEST SUITE{X}")
    print(f"{B}{'═'*70}{X}")

    section_bench_status()
    section_bench_accuracy()
    section_utilization_band()
    section_burnout_label()
    section_health_scores()
    section_health_warnings()
    section_health_aware_score()
    section_org_summaries()
    section_edge_cases()
    section_pipeline_regression()
    section_stress()

    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    failed = len(_RESULTS) - passed

    print(f"\n{B}{'═'*70}{X}")
    colour = G if failed == 0 else R
    print(f"{B}  Results: {colour}{passed} passed{X}{B}, "
          f"{R if failed else G}{failed} failed{X}  "
          f"(total {len(_RESULTS)} checks){X}")
    print(f"{B}{'═'*70}{X}\n")

    if failed:
        sys.exit(1)