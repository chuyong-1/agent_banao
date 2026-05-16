"""
bench.py — Bench Availability Engine  (NEW — v3 Additive Extension)
====================================================================
Identifies fully-benched, partially-benched, and soon-to-be-benched employees.

STRICTLY ADDITIVE: this module does NOT modify any existing node, model,
scoring formula, or ranking logic.  It is called from compute_metrics_node
(nodes.py) and its output is appended as an extra field on each metrics dict
entry.  All existing tests, outputs, and integrations are unaffected.

Configuration (all optional, env-var driven):
    BENCH_THRESHOLD_HOURS   Free h/wk at or above which an employee is
                            considered "partially benched" (default: 10.0)
    ROLLING_OFF_WEEKS       Window in weeks within which all assignments must
                            end to classify as ROLLING_OFF_SOON (default: 2)
    ENABLE_BENCH_BOOST      Set to "true" to apply an availability score boost
                            to AVAILABLE_NOW employees in ranking (default: false)
    BENCH_AVAILABILITY_BOOST  Boost magnitude added to availability_score when
                            ENABLE_BENCH_BOOST is active (default: 5.0, capped
                            so the score never exceeds 100)
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from state import BenchMetrics, BenchStatus

# ── Configurable thresholds ────────────────────────────────────────────────────
BENCH_THRESHOLD_HOURS: float = float(os.getenv("BENCH_THRESHOLD_HOURS", "10.0"))
ROLLING_OFF_WEEKS:     int   = int(os.getenv("ROLLING_OFF_WEEKS", "2"))
ENABLE_BENCH_BOOST:    bool  = os.getenv("ENABLE_BENCH_BOOST", "false").lower() == "true"
BENCH_AVAILABILITY_BOOST: float = float(os.getenv("BENCH_AVAILABILITY_BOOST", "5.0"))


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_date(s: str) -> date:
    from datetime import datetime
    return datetime.strptime(s, "%Y-%m-%d").date()


def _iso(d: date) -> str:
    return d.isoformat()


def _top_skills(skills: List[Dict[str, Any]], top_n: int = 5) -> List[str]:
    """Return the employee's top N skills ordered by proficiency (desc)."""
    sorted_s = sorted(skills, key=lambda s: s.get("proficiency", 0), reverse=True)
    return [s["name"] for s in sorted_s[:top_n]]


# ── Public API ────────────────────────────────────────────────────────────────

def compute_bench_metrics(
    employee:                Dict[str, Any],
    active_assignments:      List[Dict[str, Any]],
    available_hours_per_week: float,
    utilization_pct:         float,
    today:                   date,
) -> BenchMetrics:
    """
    Compute bench status and related metrics for a single employee.

    Parameters
    ----------
    employee                 : Raw employee dict from ERP data
                               (id, name, role, skills, capacity_hours_per_week).
    active_assignments       : Assignments still running on or after today;
                               pre-indexed by compute_metrics_node (no re-scan).
    available_hours_per_week : Reused from compute_metrics_node — not recomputed.
    utilization_pct          : Reused from compute_metrics_node — not recomputed.
    today                    : Reference date (injected for testability).

    Returns
    -------
    BenchMetrics — fully populated Pydantic model ready for .model_dump().
    """
    capacity = employee.get("capacity_hours_per_week", 0.0)

    # ── Last project end date ──────────────────────────────────────────────
    last_project_end_date: Optional[str] = None
    if active_assignments:
        latest_end = max(_parse_date(a["end_date"]) for a in active_assignments)
        last_project_end_date = _iso(latest_end)

    # ── Bench status (priority order) ─────────────────────────────────────
    rolling_off_cutoff = today + timedelta(weeks=ROLLING_OFF_WEEKS)

    if utilization_pct > 100.0:
        bench_status = BenchStatus.OVERALLOCATED

    elif capacity <= 0.0 or (not active_assignments and available_hours_per_week <= 0):
        bench_status = BenchStatus.AVAILABLE_NOW

    elif not active_assignments:
        # No active assignments but positive capacity → fully benched
        bench_status = BenchStatus.AVAILABLE_NOW

    else:
        all_ending_soon = all(
            _parse_date(a["end_date"]) <= rolling_off_cutoff
            for a in active_assignments
        )
        if all_ending_soon:
            bench_status = BenchStatus.ROLLING_OFF_SOON
        elif available_hours_per_week >= BENCH_THRESHOLD_HOURS:
            bench_status = BenchStatus.PARTIALLY_ALLOCATED
        else:
            bench_status = BenchStatus.FULLY_ALLOCATED

    # ── Bench percentage (fraction of capacity that is free) ───────────────
    bench_percentage = round(
        (available_hours_per_week / max(1.0, capacity)) * 100.0, 1
    )
    bench_percentage = max(0.0, min(100.0, bench_percentage))

    # ── Projected bench date & days-until-bench ────────────────────────────
    if bench_status == BenchStatus.AVAILABLE_NOW:
        projected_bench_date = _iso(today)
        bench_duration_days  = 0    # already on bench

    elif bench_status in (
        BenchStatus.ROLLING_OFF_SOON,
        BenchStatus.FULLY_ALLOCATED,
        BenchStatus.PARTIALLY_ALLOCATED,
        BenchStatus.OVERALLOCATED,
    ):
        if active_assignments:
            latest_end           = max(_parse_date(a["end_date"]) for a in active_assignments)
            projected_bench_date = _iso(latest_end + timedelta(days=1))
            bench_duration_days  = max(0, (latest_end - today).days)
        else:
            projected_bench_date = _iso(today)
            bench_duration_days  = 0

    else:
        projected_bench_date = _iso(today)
        bench_duration_days  = 0

    # ── Primary skill cluster ──────────────────────────────────────────────
    primary_skill_cluster = _top_skills(employee.get("skills", []))

    return BenchMetrics(
        bench_status          = bench_status,
        bench_percentage      = bench_percentage,
        bench_duration_days   = bench_duration_days,
        projected_bench_date  = projected_bench_date,
        last_project_end_date = last_project_end_date,
        primary_skill_cluster = primary_skill_cluster,
    )


def compute_bench_summary(
    all_metrics: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Aggregate bench metrics across all employees into an org-level summary.

    Called by server.py to build the ``bench_summary`` section of the UI
    payload.  Only employees whose metrics dict contains a ``bench_metrics``
    key (i.e. those processed by the extended compute_metrics_node) are
    included; missing keys are silently skipped for backward compatibility.

    Returns
    -------
    Dict with total_bench_capacity, counts per status category, and the
    detail lists (available_now, partially_allocated, rolling_off_soon).
    """
    available_now_list:   List[Dict[str, Any]] = []
    partially_list:       List[Dict[str, Any]] = []
    rolling_off_list:     List[Dict[str, Any]] = []
    fully_allocated_list: List[Dict[str, Any]] = []
    overallocated_list:   List[Dict[str, Any]] = []
    total_bench_cap = 0.0

    for m in all_metrics:
        bm = m.get("bench_metrics")
        if not bm:
            continue
        status = bm.get("bench_status", BenchStatus.FULLY_ALLOCATED)
        avail  = m.get("available_hours_per_week", 0.0)
        total_bench_cap += max(0.0, avail)

        entry: Dict[str, Any] = {
            "employee_id":              m["employee_id"],
            "name":                     m["name"],
            "role":                     m["role"],
            "bench_status":             status,
            "bench_percentage":         bm.get("bench_percentage", 0.0),
            "available_hours_per_week": avail,
            "projected_bench_date":     bm.get("projected_bench_date", ""),
            "last_project_end_date":    bm.get("last_project_end_date"),
            "primary_skill_cluster":    bm.get("primary_skill_cluster", []),
        }

        if status == BenchStatus.AVAILABLE_NOW:
            available_now_list.append(entry)
        elif status == BenchStatus.PARTIALLY_ALLOCATED:
            partially_list.append(entry)
        elif status == BenchStatus.ROLLING_OFF_SOON:
            rolling_off_list.append(entry)
        elif status == BenchStatus.OVERALLOCATED:
            overallocated_list.append(entry)
        else:
            fully_allocated_list.append(entry)

    # Sort each list by available hours desc for easy scanning
    for lst in (available_now_list, partially_list, rolling_off_list):
        lst.sort(key=lambda e: e["available_hours_per_week"], reverse=True)

    return {
        "total_bench_capacity_hours_per_week": round(total_bench_cap, 1),
        "available_now_count":       len(available_now_list),
        "partially_allocated_count": len(partially_list),
        "rolling_off_soon_count":    len(rolling_off_list),
        "fully_allocated_count":     len(fully_allocated_list),
        "overallocated_count":       len(overallocated_list),
        "bench_threshold_hours":     BENCH_THRESHOLD_HOURS,
        "rolling_off_weeks":         ROLLING_OFF_WEEKS,
        "available_now":             available_now_list,
        "partially_allocated":       partially_list,
        "rolling_off_soon":          rolling_off_list,
    }