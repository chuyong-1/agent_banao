"""
health.py — Workforce / Workload Health Engine  (NEW — v3 Additive Extension)
==============================================================================
Computes per-employee workforce health metrics and org-level summaries.

STRICTLY ADDITIVE: this module does NOT modify any existing node, model,
scoring formula, or ranking logic.  It is called from compute_metrics_node
(nodes.py) and its output is appended as an extra field on each metrics dict.

Existing reliability_score, fit_score, and all ranking behaviour are preserved
exactly.  Health-aware staffing is opt-in via HEALTH_AWARE_SCORING=true.

────────────────────────────────────────────────────────────────────────────────
Scoring Formulas
────────────────────────────────────────────────────────────────────────────────

workload_score (0–100, higher = lighter / healthier load)
─────────────────────────────────────────────────────────
    base     = max(0, 100 − utilization_pct)
    penalty  = 5 × overdue + 3 × effectively_overdue
    penalty += 10 if overallocated
    result   = clamp(base − penalty, 0, 100)

burnout_risk_score (0–100, higher = more risk)
──────────────────────────────────────────────
    base  = min(100, utilization_pct)
    base += 10 × overdue
    base += 5  × effectively_overdue
    base += 5  if overallocated
    base += 5  if leave_overlap_pct > 50 AND utilization > 75
    base += 5  if in_progress_bounties >= 3  (context-switch risk)
    result = clamp(base, 0, 100)

sustainability_score (0–100)
────────────────────────────
    = clamp(workload_score × 0.6 + (100 − burnout_risk_score) × 0.4, 0, 100)

────────────────────────────────────────────────────────────────────────────────
Configuration (all optional, env-var driven)
────────────────────────────────────────────────────────────────────────────────
    UNDERUTILIZED_MAX    Max util% to be UNDERUTILIZED band   (default 25)
    HEALTHY_MAX          Max util% for HEALTHY band           (default 75)
    HIGH_UTILIZATION_MAX Max util% for HIGH_UTILIZATION band  (default 90)
    OVERLOADED_MAX       Max util% for OVERLOADED band        (default 100)
    BURNOUT_LOW_MAX      Max burnout score for LOW risk       (default 30)
    BURNOUT_MODERATE_MAX Max burnout score for MODERATE risk  (default 60)
    BURNOUT_HIGH_MAX     Max burnout score for HIGH risk      (default 80)

    HEALTH_AWARE_SCORING     Set "true" to enable health dimension in ranking
    HEALTH_W_AVAILABILITY    Weight for availability in health-aware mode   (default 0.35)
    HEALTH_W_SKILL           Weight for skill match in health-aware mode    (default 0.25)
    HEALTH_W_RELIABILITY     Weight for reliability in health-aware mode    (default 0.20)
    HEALTH_W_HEALTH          Weight for health/sustainability in h-a mode   (default 0.20)
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, List

from state import BurnoutRisk, UtilizationBand, WorkforceHealthMetrics

# ── Utilization band thresholds ────────────────────────────────────────────────
UNDERUTILIZED_MAX:    float = float(os.getenv("UNDERUTILIZED_MAX",    "25.0"))
HEALTHY_MAX:          float = float(os.getenv("HEALTHY_MAX",          "75.0"))
HIGH_UTILIZATION_MAX: float = float(os.getenv("HIGH_UTILIZATION_MAX", "90.0"))
OVERLOADED_MAX:       float = float(os.getenv("OVERLOADED_MAX",      "100.0"))

# ── Burnout risk thresholds ────────────────────────────────────────────────────
BURNOUT_LOW_MAX:      float = float(os.getenv("BURNOUT_LOW_MAX",      "30.0"))
BURNOUT_MODERATE_MAX: float = float(os.getenv("BURNOUT_MODERATE_MAX", "60.0"))
BURNOUT_HIGH_MAX:     float = float(os.getenv("BURNOUT_HIGH_MAX",     "80.0"))

# ── Health-aware scoring configuration ────────────────────────────────────────
HEALTH_AWARE_SCORING:  bool  = os.getenv("HEALTH_AWARE_SCORING", "false").lower() == "true"
HEALTH_W_AVAILABILITY: float = float(os.getenv("HEALTH_W_AVAILABILITY", "0.35"))
HEALTH_W_SKILL:        float = float(os.getenv("HEALTH_W_SKILL",        "0.25"))
HEALTH_W_RELIABILITY:  float = float(os.getenv("HEALTH_W_RELIABILITY",  "0.20"))
HEALTH_W_HEALTH:       float = float(os.getenv("HEALTH_W_HEALTH",       "0.20"))


# ── Internal helpers ──────────────────────────────────────────────────────────

def _utilization_band(utilization_pct: float) -> UtilizationBand:
    if utilization_pct <= UNDERUTILIZED_MAX:
        return UtilizationBand.UNDERUTILIZED
    if utilization_pct <= HEALTHY_MAX:
        return UtilizationBand.HEALTHY
    if utilization_pct <= HIGH_UTILIZATION_MAX:
        return UtilizationBand.HIGH_UTILIZATION
    if utilization_pct <= OVERLOADED_MAX:
        return UtilizationBand.OVERLOADED
    return UtilizationBand.CRITICAL


def _burnout_risk_label(score: float) -> BurnoutRisk:
    if score <= BURNOUT_LOW_MAX:
        return BurnoutRisk.LOW
    if score <= BURNOUT_MODERATE_MAX:
        return BurnoutRisk.MODERATE
    if score <= BURNOUT_HIGH_MAX:
        return BurnoutRisk.HIGH
    return BurnoutRisk.CRITICAL


# ── Public API ────────────────────────────────────────────────────────────────

def compute_workforce_health(
    employee_metric: Dict[str, Any],
    today: date,  # noqa: ARG001  (kept for signature parity / future date-relative heuristics)
) -> WorkforceHealthMetrics:
    """
    Compute workforce health metrics for a single employee.

    ``employee_metric`` is the dict produced by EmployeeMetrics.model_dump()
    inside compute_metrics_node — no re-computation; purely additive.

    Parameters
    ----------
    employee_metric : Dict produced by EmployeeMetrics.model_dump().
    today           : Reference date (injected for testability and future
                      date-relative heuristics such as consecutive-project-
                      streak detection).

    Returns
    -------
    WorkforceHealthMetrics — fully populated Pydantic model.
    """
    util_pct    = employee_metric.get("utilization_pct", 0.0)
    overalloc   = employee_metric.get("overallocation_flag", False)
    avail_h     = employee_metric.get("available_hours_per_week", 0.0)
    bm          = employee_metric.get("bounty_metrics", {})
    ld          = employee_metric.get("leave_detail", {})

    overdue     = bm.get("overdue", 0)
    eff_overdue = bm.get("effectively_overdue", 0)
    in_progress = bm.get("in_progress", 0)
    leave_pct   = ld.get("overlap_pct", 0.0)
    reliability = bm.get("reliability_score", 70.0)

    # ── workload_score ─────────────────────────────────────────────────────
    workload  = max(0.0, 100.0 - util_pct)
    workload -= 5.0 * overdue
    workload -= 3.0 * eff_overdue
    if overalloc:
        workload -= 10.0
    workload = round(max(0.0, min(100.0, workload)), 1)

    # ── burnout_risk_score ─────────────────────────────────────────────────
    burnout  = min(100.0, util_pct)
    burnout += 10.0 * overdue
    burnout +=  5.0 * eff_overdue
    if overalloc:
        burnout += 5.0
    if leave_pct > 50.0 and util_pct > 75.0:
        burnout += 5.0      # approved leave on top of already high load
    if in_progress >= 3:
        burnout += 5.0      # heavy multi-task context switching
    burnout = round(max(0.0, min(100.0, burnout)), 1)

    # ── sustainability_score ───────────────────────────────────────────────
    sustainability = round(workload * 0.6 + (100.0 - burnout) * 0.4, 1)
    sustainability = max(0.0, min(100.0, sustainability))

    # ── utilization band ───────────────────────────────────────────────────
    band = _utilization_band(util_pct)

    # ── health warnings ────────────────────────────────────────────────────
    warnings: List[str] = []

    if burnout >= BURNOUT_HIGH_MAX:
        warnings.append("🔴 High burnout probability")
    if avail_h < 5.0 and not overalloc:
        warnings.append("⚠️ Insufficient recovery bandwidth (< 5 h/wk free)")
    if overalloc:
        warnings.append("🔴 Sustained overallocation detected")
    if band == UtilizationBand.UNDERUTILIZED:
        warnings.append("💤 Bench underutilization — resource may disengage")
    if overdue + eff_overdue >= 2:
        warnings.append(
            f"⏰ Repeated overdue work ({overdue + eff_overdue} tasks) "
            "— reliability trend degrading"
        )
    if in_progress >= 3:
        warnings.append(
            f"📋 Multiple concurrent bounties ({in_progress}) "
            "— context-switching risk"
        )
    if util_pct >= 90.0 and reliability < 70.0:
        warnings.append(
            "📉 Sustained >90% utilisation with declining reliability "
            "— burnout signal"
        )

    burnout_label = _burnout_risk_label(burnout)

    return WorkforceHealthMetrics(
        workload_score       = workload,
        burnout_risk_score   = burnout,
        burnout_risk         = burnout_label,
        sustainability_score = sustainability,
        utilization_band     = band,
        health_warnings      = warnings,
    )


def compute_health_summary(
    all_metrics: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Aggregate workforce health across all employees into an org-level summary.

    Called by server.py to build the ``workforce_health_summary`` section of
    the UI payload.  Only employees whose metrics dict contains a
    ``workforce_health`` key are included; missing keys are skipped for
    backward compatibility.

    Returns
    -------
    Dict with average utilisation, counts per risk category, and a list of
    employees with HIGH / CRITICAL burnout risk for immediate action.
    """
    total = len(all_metrics)
    if not total:
        return {}

    overloaded_count    = 0
    burnout_risk_count  = 0
    underutil_count     = 0
    total_util          = 0.0
    burnout_alerts: List[Dict[str, Any]] = []
    utilization_heat: List[Dict[str, Any]] = []  # all employees, sorted by util desc

    for m in all_metrics:
        wh = m.get("workforce_health")
        if not wh:
            continue
        util_pct = m.get("utilization_pct", 0.0)
        total_util += util_pct
        band    = wh.get("utilization_band", UtilizationBand.HEALTHY)
        burnout = wh.get("burnout_risk_score", 0.0)

        if band in (UtilizationBand.OVERLOADED, UtilizationBand.CRITICAL):
            overloaded_count += 1
        if band == UtilizationBand.UNDERUTILIZED:
            underutil_count += 1
        if burnout > BURNOUT_MODERATE_MAX:
            burnout_risk_count += 1
            burnout_alerts.append({
                "employee_id":     m["employee_id"],
                "name":            m["name"],
                "burnout_risk":    wh.get("burnout_risk", BurnoutRisk.MODERATE),
                "burnout_score":   burnout,
                "utilization_pct": util_pct,
                "warnings":        wh.get("health_warnings", []),
            })

        utilization_heat.append({
            "employee_id":       m["employee_id"],
            "name":              m["name"],
            "utilization_pct":   util_pct,
            "utilization_band":  band,
            "workload_score":    wh.get("workload_score", 0.0),
            "burnout_score":     burnout,
            "sustainability":    wh.get("sustainability_score", 0.0),
        })

    avg_util = round(total_util / total, 1) if total else 0.0

    # Sort burnout alerts by risk score desc for at-a-glance prioritisation
    burnout_alerts.sort(key=lambda e: e["burnout_score"], reverse=True)
    utilization_heat.sort(key=lambda e: e["utilization_pct"], reverse=True)

    return {
        "total_employees_evaluated":    total,
        "average_utilization_pct":      avg_util,
        "overloaded_employee_count":    overloaded_count,
        "burnout_risk_count":           burnout_risk_count,
        "underutilized_employee_count": underutil_count,
        "burnout_alerts":               burnout_alerts,
        "utilization_heat":             utilization_heat[:20],  # top 20 for payload
        "health_aware_scoring_enabled": HEALTH_AWARE_SCORING,
        "scoring_weights": {
            "availability":  f"{HEALTH_W_AVAILABILITY:.0%}" if HEALTH_AWARE_SCORING else "45% (default)",
            "skill_match":   f"{HEALTH_W_SKILL:.0%}"        if HEALTH_AWARE_SCORING else "30% (default)",
            "reliability":   f"{HEALTH_W_RELIABILITY:.0%}"  if HEALTH_AWARE_SCORING else "25% (default)",
            "health":        f"{HEALTH_W_HEALTH:.0%}"       if HEALTH_AWARE_SCORING else "0% (disabled)",
        },
    }


def health_aware_fit_score(
    availability_score: float,
    skill_score:        float,
    reliability_score:  float,
    sustainability_score: float,
) -> float:
    """
    Health-aware fit score using configurable weights.

    Only called when HEALTH_AWARE_SCORING=true.  In default mode the existing
    W_AVAILABILITY/W_SKILL/W_RELIABILITY weights in matchmaker_node are used
    unchanged — this function is never invoked.

    Returns
    -------
    float — rounded to 1 dp, guaranteed in [0, 100].
    """
    score = (
        HEALTH_W_AVAILABILITY * availability_score
        + HEALTH_W_SKILL        * skill_score
        + HEALTH_W_RELIABILITY  * reliability_score
        + HEALTH_W_HEALTH       * sustainability_score
    )
    return round(max(0.0, min(100.0, score)), 1)

# ══════════════════════════════════════════════════════════════════════════════
# v4 — DB PERSISTENCE HELPERS  (called by monthly_job.py)
# ══════════════════════════════════════════════════════════════════════════════

import logging as _logging
_logger = _logging.getLogger(__name__)


def build_workforce_health_blob(
    health_metrics,
    growth_components,
    cell_data=None,
    assessment_data=None,
):
    """
    Build the unified JSONB payload for growth_snapshots.workforce_health.

    Merges STROMA's growth penalty components with the LangGraph pipeline's
    per-employee WorkforceHealthMetrics into a single dict stored as JSONB:

        {
          "components":     {completion_penalty, carry_penalty, ...},
          "cell_data":      {raw CELL summary},
          "assessment":     {raw assessment result},
          "planner_health": {WorkforceHealthMetrics fields}
        }
    """
    if hasattr(health_metrics, "model_dump"):
        planner_dump = health_metrics.model_dump()
    else:
        planner_dump = dict(health_metrics)

    return {
        "components":     growth_components,
        "cell_data":      cell_data or {},
        "assessment":     assessment_data or {},
        "planner_health": planner_dump,
    }


def upsert_health_into_snapshot(db, employee_id, snapshot_month, blob):
    """
    Merge ``blob`` into growth_snapshots.workforce_health via a JSONB ||
    UPDATE so existing keys are preserved and new keys overwrite.

    The row must already exist (written by monthly_job step 5).
    If no row is found this is a no-op + warning — never raises.
    """
    import json
    from sqlalchemy import text

    result = db.execute(
        text(
            """
            UPDATE growth_snapshots
               SET workforce_health = workforce_health || :blob ::jsonb
             WHERE employee_id    = :eid
               AND snapshot_month = :month
            """
        ),
        {
            "blob":  json.dumps(blob),
            "eid":   employee_id,
            "month": snapshot_month,
        },
    )

    if result.rowcount == 0:
        _logger.warning(
            "[health] upsert_health_into_snapshot: no row for employee_id=%s "
            "snapshot_month=%s — skipping merge.",
            employee_id, snapshot_month,
        )