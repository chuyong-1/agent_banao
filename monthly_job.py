"""
monthly_job.py — The STROMA Brain  (Phase 3)
=============================================
Runs on the 1st of every month via a cron scheduler (e.g. APScheduler,
Celery Beat, or a plain cron entry: ``0 0 1 * * python monthly_job.py``).

Orchestration steps
───────────────────
  Step 1  Load all active employees from PostgreSQL.
  Step 2  Fetch CELL summaries (mock implementation; swap for real HTTP/DB call).
  Step 3  Load prior-month growth scores + most recent assessment scores.
  Step 4  Compute Growth Score per employee using the deterministic penalty algorithm.
  Step 5  Upsert the base GrowthSnapshot row, then deep-merge the full health blob.
  Step 6  Run Stage Gate check — flag gates ≤ STAGE_GATE_WARNING_DAYS away,
          subtracting approved leave days so absence does not penalise the intern's
          evaluation clock (the "Time-Freezing" fix).
  Step 7  Run Leech Detection — flag interns past 6 months with flat/declining
          trajectories and low growth scores.
  Step 8  Write an immutable STROMAAction audit row.

Growth Score algorithm (deterministic, 0–100)
─────────────────────────────────────────────
  completion_penalty = (1 − completion_rate)   × COMPLETION_WEIGHT   [max 30]
  carry_penalty      = carry_rate              × CARRY_WEIGHT         [max 20]
  eod_penalty        = (1 − eod_compliance)    × EOD_WEIGHT           [max 15]
  assessment_penalty = max(0, (BASELINE−score) / BASELINE)
                       × ASSESSMENT_WEIGHT  (0 if no assessment)      [max 20]
  velocity_penalty   = max(0, (TARGET−completed) / TARGET)
                       × VELOCITY_WEIGHT                               [max 15]
  growth_score       = clamp(100 − sum_of_penalties, 0, 100)

Stage Gate timing (with leave clock-pause)
──────────────────────────────────────────
  active_days = calendar_days_in_stage
              − weekday leave days approved in that same window
  days_remaining = stage_duration − active_days
  Flag when days_remaining ≤ STAGE_GATE_WARNING_DAYS.

Leech Detection conditions (ALL must be true)
─────────────────────────────────────────────
  1. current_stage ∈ {intern_bounty, intern_hybrid}
  2. (today − join_date).days > LEECH_THRESHOLD_DAYS  (default 180 = 6 months)
  3. growth_trajectory ∈ {"stable", "declining"}
  4. growth_score < BAND_STRONG  (< 75)

Configuration (all env-var driven with sane defaults)
──────────────────────────────────────────────────────
  COMPLETION_WEIGHT      30     Penalty weight for completion_rate shortfall.
  CARRY_WEIGHT           20     Penalty weight for bounties carried forward.
  EOD_WEIGHT             15     Penalty weight for EOD non-compliance.
  ASSESSMENT_WEIGHT      20     Penalty weight for below-baseline assessment.
  VELOCITY_WEIGHT        15     Penalty weight for below-target velocity.
  ASSESSMENT_BASELINE    75     Score below which assessment_penalty applies.
  VELOCITY_TARGET        10     Bounties/month above which velocity_penalty = 0.
  BAND_STRONG            75     growth_score ≥ this → "strong" band.
  BAND_DEVELOPING        50     growth_score ≥ this → "developing" band.
  STAGE_GATE_WARNING_DAYS    14   Flag gate this many days before due date.
  STAGE_GATE_ESCALATION_DAYS  7   Escalate if still open this many days past due.
  LEECH_THRESHOLD_DAYS      180   Days past join_date before leech check activates.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from db import _business_days, session_scope
from health import build_workforce_health_blob, upsert_health_into_snapshot
from models import (
    AssessmentResult,
    GrowthSnapshot,
    LeechFlag,
    Leave as LeaveRow,
    Person,
    StageGateFlag,
    StrOMAAction as STROMAAction,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Growth score penalty weights
COMPLETION_WEIGHT:   float = float(os.getenv("COMPLETION_WEIGHT",   "30"))
CARRY_WEIGHT:        float = float(os.getenv("CARRY_WEIGHT",        "20"))
EOD_WEIGHT:          float = float(os.getenv("EOD_WEIGHT",          "15"))
ASSESSMENT_WEIGHT:   float = float(os.getenv("ASSESSMENT_WEIGHT",   "20"))
VELOCITY_WEIGHT:     float = float(os.getenv("VELOCITY_WEIGHT",     "15"))
ASSESSMENT_BASELINE: float = float(os.getenv("ASSESSMENT_BASELINE", "75"))
VELOCITY_TARGET:     float = float(os.getenv("VELOCITY_TARGET",     "10"))

# Growth band thresholds
BAND_STRONG:     int = int(os.getenv("BAND_STRONG",     "75"))
BAND_DEVELOPING: int = int(os.getenv("BAND_DEVELOPING", "50"))

# Stage gate
STAGE_GATE_WARNING_DAYS:    int = int(os.getenv("STAGE_GATE_WARNING_DAYS",    "14"))
STAGE_GATE_ESCALATION_DAYS: int = int(os.getenv("STAGE_GATE_ESCALATION_DAYS", "7"))

# Leech detection
LEECH_THRESHOLD_DAYS: int = int(os.getenv("LEECH_THRESHOLD_DAYS", "180"))

# Lifecycle stages that have a defined gate duration (in days).
# Stages mapped to None have no automatic gate.
STAGE_DURATIONS: Dict[str, Optional[int]] = {
    "intern_bounty": 90,
    "intern_hybrid": 90,
    "full_time":     None,
    "apm":           None,
    "tech_lead":     None,
    "pm":            None,
    "dept_head":     None,
}

INTERN_STAGES = frozenset({"intern_bounty", "intern_hybrid"})


# ─────────────────────────────────────────────────────────────────────────────
# Slack alerting helper  (mock implementation)
# ─────────────────────────────────────────────────────────────────────────────

def push_slack_alert(message: str) -> None:
    """
    Send a notification to the STROMA Slack channel.

    ── Production replacement ───────────────────────────────────────────────
    Replace this function body with a real Slack webhook call:
        import httpx
        SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
        if SLACK_WEBHOOK_URL:
            httpx.post(SLACK_WEBHOOK_URL, json={"text": message})

    ── Current behaviour ───────────────────────────────────────────────────
    Logs the alert at WARNING level so it is visible in the job output.
    This is a mock implementation sufficient for local development and
    integration testing.
    """
    logger.warning("[SlackAlert] (mock) %s", message)


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — CELL summary fetcher  (mock implementation)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_cell_summaries(employee_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Fetch per-employee CELL metrics for the current month.

    ── Production replacement ───────────────────────────────────────────────
    Replace this function body with a real data source.  Options:
      • HTTP GET to the CELL API:
            resp = httpx.get(f"{CELL_BASE_URL}/summaries",
                             params={"ids": ",".join(employee_ids)},
                             headers={"Authorization": f"Bearer {CELL_TOKEN}"})
            return resp.json()
      • Direct DB query against the CELL integration table:
            rows = db.execute(select(CellSummary).where(...)).all()
            return {r.employee_id: r.to_dict() for r in rows}

    ── Mock strategy ────────────────────────────────────────────────────────
    Values are derived deterministically from a hash of employee_id so that
    unit tests can assert on specific outputs without randomness.  The hash
    produces consistent values across interpreter restarts (Python's built-in
    hash() is not stable; we use a simple polynomial instead).

    Returned keys per employee
    ──────────────────────────
    completion_rate      float 0.0–1.0  bounties completed / assigned this month
    carry_rate           float 0.0–1.0  bounties carried forward from last month
    eod_compliance_rate  float 0.0–1.0  daily check-ins submitted before EOD
    bounties_completed   int            count completed this month
    bounties_assigned    int            count assigned this month
    """
    results: Dict[str, Dict[str, Any]] = {}
    for eid in employee_ids:
        # Stable polynomial hash of employee_id string (avoids Python hash() randomisation)
        h = 0
        for ch in eid:
            h = (h * 31 + ord(ch)) & 0xFFFF   # 0–65535, stable across runs

        results[eid] = {
            "completion_rate":     round(0.50 + (h % 1000) / 2000, 3),   # 0.50–1.00
            "carry_rate":          round((h % 300) / 1000, 3),            # 0.00–0.30
            "eod_compliance_rate": round(0.60 + (h % 800) / 2000, 3),    # 0.60–1.00
            "bounties_completed":  int(4 + h % 8),                        # 4–11
            "bounties_assigned":   int(5 + h % 10),                       # 5–14
        }
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 / 4 — Deterministic Growth Score
# ─────────────────────────────────────────────────────────────────────────────

def compute_growth_score(
    cell:               Dict[str, Any],
    assessment_score:   Optional[int],
    prior_growth_score: Optional[int],
) -> Dict[str, Any]:
    """
    Compute this month's growth score from CELL data and the most recent
    assessment.

    Algorithm — all penalties subtracted from a base of 100:

        completion_penalty  = (1 − completion_rate)   × COMPLETION_WEIGHT
        carry_penalty       = carry_rate              × CARRY_WEIGHT
        eod_penalty         = (1 − eod_compliance)    × EOD_WEIGHT
        assessment_penalty  = max(0, (BASELINE − score) / BASELINE)
                              × ASSESSMENT_WEIGHT   [0 if no assessment]
        velocity_penalty    = max(0, (TARGET − bounties_completed) / TARGET)
                              × VELOCITY_WEIGHT

        growth_score = clamp(100 − Σpenalties, 0, 100)

    Parameters
    ──────────
    cell               CELL summary dict (from fetch_cell_summaries).
    assessment_score   Most recent assessment score (0–100), or None.
    prior_growth_score Last month's growth_score, or None (first snapshot).

    Returns
    ───────
    Dict with keys:
        score       int (0–100)
        band        "strong" | "developing" | "at_risk"
        trajectory  "improving" | "stable" | "declining" | None
        components  dict of individual penalty values (stored in JSONB)
    """
    completion_rate     = float(cell.get("completion_rate",     1.0))
    carry_rate          = float(cell.get("carry_rate",          0.0))
    eod_compliance_rate = float(cell.get("eod_compliance_rate", 1.0))
    bounties_completed  = int(cell.get("bounties_completed",    0))

    # ── Individual penalties ───────────────────────────────────────────────
    completion_penalty = round((1.0 - completion_rate)   * COMPLETION_WEIGHT, 1)
    carry_penalty      = round(carry_rate                * CARRY_WEIGHT,      1)
    eod_penalty        = round((1.0 - eod_compliance_rate) * EOD_WEIGHT,      1)

    if assessment_score is not None:
        shortfall          = max(0.0, ASSESSMENT_BASELINE - assessment_score)
        assessment_penalty = round(
            (shortfall / max(1.0, ASSESSMENT_BASELINE)) * ASSESSMENT_WEIGHT, 1
        )
    else:
        assessment_penalty = 0.0

    velocity_gap     = max(0.0, VELOCITY_TARGET - bounties_completed)
    velocity_penalty = round(
        (velocity_gap / max(1.0, VELOCITY_TARGET)) * VELOCITY_WEIGHT, 1
    )

    total_penalty = (
        completion_penalty
        + carry_penalty
        + eod_penalty
        + assessment_penalty
        + velocity_penalty
    )
    growth_score = int(max(0, min(100, round(100.0 - total_penalty))))

    # ── Band ──────────────────────────────────────────────────────────────
    if growth_score >= BAND_STRONG:
        band = "strong"
    elif growth_score >= BAND_DEVELOPING:
        band = "developing"
    else:
        band = "at_risk"

    # ── Trajectory (vs prior month; ±3 pt dead-band prevents noise) ───────
    if prior_growth_score is None:
        trajectory = None
    elif growth_score >= prior_growth_score + 3:
        trajectory = "improving"
    elif growth_score <= prior_growth_score - 3:
        trajectory = "declining"
    else:
        trajectory = "stable"

    return {
        "score":      growth_score,
        "band":       band,
        "trajectory": trajectory,
        "components": {
            "completion_penalty":  completion_penalty,
            "carry_penalty":       carry_penalty,
            "eod_penalty":         eod_penalty,
            "assessment_penalty":  assessment_penalty,
            "velocity_penalty":    velocity_penalty,
            "total_penalty":       round(total_penalty, 1),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Step 6 helpers — Stage Gate
# ─────────────────────────────────────────────────────────────────────────────

def _approved_leave_days_in_window(
    db:           Any,    # sqlalchemy.orm.Session
    employee_id:  str,
    window_start: date,
    window_end:   date,
) -> int:
    """
    Count the number of approved weekday leave days for *employee_id* that
    overlap the window [window_start, window_end].

    This value is subtracted from the calendar-days-in-stage count to
    "pause" the evaluation clock during legitimate absences.

    Two leave records whose date ranges overlap inside the window are each
    counted independently (no interval merging needed here because the
    purpose is to credit the employee for absence days, not to compute
    a percentage — double-counting is impossible as each calendar day can
    only be on leave once in the payroll system).

    Uses _business_days() so weekends inside a leave record do not inflate
    the credit.  Public holidays are not subtracted a second time here
    (they are already excluded from the stage gate's weekday-day count).
    """
    rows = db.execute(
        select(LeaveRow.start_date, LeaveRow.end_date).where(
            LeaveRow.employee_id == employee_id,
            LeaveRow.start_date  <= window_end,
            LeaveRow.end_date    >= window_start,
        )
    ).all()

    total = 0
    for row in rows:
        # Clip the leave record to the evaluation window before counting.
        overlap_start = max(row[0], window_start)
        overlap_end   = min(row[1], window_end)
        if overlap_start <= overlap_end:
            total += _business_days(overlap_start, overlap_end)
    return total


def check_stage_gate(
    db:     Any,    # sqlalchemy.orm.Session
    person: Any,    # models.Person ORM row
    today:  date,
) -> Optional[Dict[str, Any]]:
    """
    Determine whether a stage gate is approaching and create a StageGateFlag
    row if so.

    ── Leave clock-pause ───────────────────────────────────────────────────
    active_days = calendar days elapsed since stage_start_date
                − approved weekday leave days in [stage_start_date, today]

    This means a 10-day PTO block adds 10 (weekday) days back onto the
    clock, preventing the intern from hitting the 90-day gate while they
    were legally absent.

    ── Gate evaluation ─────────────────────────────────────────────────────
    days_remaining = stage_duration − active_days
    gate_due_date  = today + max(0, days_remaining)

    Flag is created when: days_remaining ≤ STAGE_GATE_WARNING_DAYS.

    ── Idempotency ─────────────────────────────────────────────────────────
    If an open flag already exists for (employee_id, from_stage):
      • No new flag is created.
      • If the flag is past its due date by > STAGE_GATE_ESCALATION_DAYS
        and has not yet been escalated, escalated_at is stamped now.

    ── outside_hire bypass ─────────────────────────────────────────────────
    Caller checks person.outside_hire before calling this function.

    Returns a summary dict if a new flag was created, else None.
    """
    stage_duration = STAGE_DURATIONS.get(person.current_stage)
    if stage_duration is None:
        return None   # Stage has no gate

    window_start = person.stage_start_date
    window_end   = today

    calendar_days   = (today - person.stage_start_date).days
    leave_days      = _approved_leave_days_in_window(
        db, person.employee_id, window_start, window_end
    )
    active_days     = max(0, calendar_days - leave_days)
    days_remaining  = stage_duration - active_days
    gate_due_date   = today + timedelta(days=max(0, days_remaining))

    if days_remaining > STAGE_GATE_WARNING_DAYS:
        return None   # Gate is not yet imminent

    # ── Check for existing open flag ──────────────────────────────────────
    existing = db.execute(
        select(StageGateFlag).where(
            StageGateFlag.employee_id == person.employee_id,
            StageGateFlag.from_stage  == person.current_stage,
            StageGateFlag.resolved    == False,       # noqa: E712
        )
    ).scalar_one_or_none()

    if existing:
        # Escalate if overdue and not yet escalated
        days_past_due = (today - existing.gate_due_date).days
        if days_past_due > STAGE_GATE_ESCALATION_DAYS and existing.escalated_at is None:
            existing.escalated_at = datetime.now(timezone.utc)
            logger.warning(
                "[StageGate] ESCALATION: employee=%s stage=%s "
                "gate_due=%s days_past_due=%d",
                person.employee_id, person.current_stage,
                existing.gate_due_date, days_past_due,
            )
        return None   # Flag already exists; no new row needed

    # ── Create new StageGateFlag ──────────────────────────────────────────
    flag = StageGateFlag(
        employee_id   = person.employee_id,
        from_stage    = person.current_stage,
        gate_due_date = gate_due_date,
        flag_sent_at  = datetime.now(timezone.utc),
        resolved      = False,
    )
    db.add(flag)

    summary = {
        "employee_id":      person.employee_id,
        "from_stage":       person.current_stage,
        "gate_due_date":    gate_due_date.isoformat(),
        "active_days":      active_days,
        "leave_subtracted": leave_days,
        "days_remaining":   days_remaining,
    }
    logger.info(
        "[StageGate] Flagged: employee=%s stage=%s active_days=%d "
        "leave_subtracted=%d days_remaining=%d gate_due=%s",
        person.employee_id, person.current_stage,
        active_days, leave_days, days_remaining, gate_due_date,
    )
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Step 7 — Leech Detection
# ─────────────────────────────────────────────────────────────────────────────

def check_leech(
    person:       Any,              # models.Person ORM row
    trajectory:   Optional[str],
    growth_score: int,
    today:        date,
) -> Optional[Dict[str, Any]]:
    """
    Detect interns retained past 6 months with flat or declining growth.

    Conditions (ALL must be true before a flag is raised):
      1. current_stage ∈ {intern_bounty, intern_hybrid}
      2. (today − join_date).days > LEECH_THRESHOLD_DAYS  (default 180)
      3. growth_trajectory ∈ {"stable", "declining"}
      4. growth_score < BAND_STRONG  (< 75)
         — An intern scoring 75+ is performing well enough that extended
           retention may be justified; skip the flag.

    Recommendation matrix:
      trajectory == "declining"  OR  growth_score < BAND_DEVELOPING  → "let_go"
      trajectory == "stable"     AND growth_score >= BAND_DEVELOPING  → "final_warning"

    The "monitor" recommendation is intentionally omitted here; STROMA
    reserves "monitor" for the case where trajectory is None (first month
    past the threshold with no prior data) — handled by the caller.

    Returns a summary dict if the conditions are met, else None.
    The caller creates the LeechFlag ORM row to allow idempotency checks.
    """
    if person.current_stage not in INTERN_STAGES:
        return None

    days_since_join = (today - person.join_date).days
    if days_since_join <= LEECH_THRESHOLD_DAYS:
        return None

    if trajectory not in ("stable", "declining"):
        # trajectory is None (first snapshot) or "improving" — not a leech
        return None

    if growth_score >= BAND_STRONG:
        # Performing strongly — extended retention may be intentional
        return None

    months_extended = max(0, (days_since_join - LEECH_THRESHOLD_DAYS) // 30)

    if trajectory == "declining" or growth_score < BAND_DEVELOPING:
        recommendation = "let_go"
    else:
        recommendation = "final_warning"

    logger.info(
        "[Leech] Flagged: employee=%s days_since_join=%d months_extended=%d "
        "trajectory=%s growth_score=%d recommendation=%s",
        person.employee_id, days_since_join, months_extended,
        trajectory, growth_score, recommendation,
    )
    return {
        "employee_id":     person.employee_id,
        "months_extended": months_extended,
        "trajectory":      trajectory,
        "recommendation":  recommendation,
        "growth_score":    growth_score,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main orchestration loop
# ─────────────────────────────────────────────────────────────────────────────

def run_monthly_job(today: Optional[date] = None) -> Dict[str, Any]:
    """
    Execute the full STROMA monthly intelligence loop.

    Parameters
    ──────────
    today   Reference date.  Defaults to date.today().  Pass an explicit
            date for manual backfills or unit-test isolation:
                run_monthly_job(today=date(2025, 4, 1))

    Returns a summary dict with counts and any per-employee error messages.
    The function never raises — errors are caught per employee and collected
    in results["errors"] so that one bad row does not abort the entire batch.

    Transaction strategy
    ────────────────────
    A single session_scope() wraps the entire job.  All ORM mutations
    (GrowthSnapshot inserts/updates, StageGateFlag rows, LeechFlag rows,
    the audit STROMAAction row) are flushed incrementally and committed
    together when the context manager exits cleanly.  On an unrecoverable
    DB error the session rolls back atomically.
    """
    if today is None:
        today = date.today()

    snapshot_month = date(today.year, today.month, 1)

    logger.info(
        "[STROMA] == Monthly job starting  snapshot_month=%s  today=%s ==",
        snapshot_month, today,
    )

    results: Dict[str, Any] = {
        "snapshot_month":      snapshot_month.isoformat(),
        "employees_processed": 0,
        "gate_flags_created":  0,
        "leech_flags_created": 0,
        "errors":              [],
    }

    with session_scope() as db:

        # ── Step 1: Load all active employees ─────────────────────────────
        people: List[Any] = (
            db.execute(select(Person).where(Person.active.is_(True)))
            .scalars()
            .all()
        )

        if not people:
            logger.warning("[STROMA] No active employees. Job complete.")
            return results

        employee_ids = [p.employee_id for p in people]
        logger.info("[STROMA] Step 1: %d active employees loaded.", len(people))

        # ── Step 2: Fetch CELL summaries ──────────────────────────────────
        cell_summaries: Dict[str, Dict[str, Any]] = fetch_cell_summaries(employee_ids)
        logger.info(
            "[STROMA] Step 2: CELL summaries fetched for %d employees.",
            len(cell_summaries),
        )

        # ── Step 3a: Most recent assessment score per employee (batch) ────
        # Single query per employee; small enough that N queries is fine.
        # For very large orgs, replace with a window-function query.
        assessment_map: Dict[str, int] = {}
        for eid in employee_ids:
            score = db.execute(
                select(AssessmentResult.score)
                .where(AssessmentResult.employee_id == eid)
                .order_by(AssessmentResult.assessment_date.desc())
                .limit(1)
            ).scalar_one_or_none()
            if score is not None:
                assessment_map[eid] = score

        # ── Step 3b: Prior-month growth scores (for trajectory) ───────────
        prior_month = (snapshot_month.replace(day=1) - timedelta(days=1)).replace(day=1)
        prior_scores: Dict[str, int] = {}
        for eid in employee_ids:
            prior = db.execute(
                select(GrowthSnapshot.growth_score).where(
                    GrowthSnapshot.employee_id    == eid,
                    GrowthSnapshot.snapshot_month == prior_month,
                )
            ).scalar_one_or_none()
            if prior is not None:
                prior_scores[eid] = prior

        logger.info(
            "[STROMA] Step 3: assessments=%d  prior_scores=%d",
            len(assessment_map), len(prior_scores),
        )

        # ── Steps 4–7: Per-employee processing loop ────────────────────────
        for person in people:
            eid = person.employee_id
            try:
                cell = cell_summaries.get(eid, {})

                # ── Step 4: Compute Growth Score ──────────────────────────
                growth_result = compute_growth_score(
                    cell               = cell,
                    assessment_score   = assessment_map.get(eid),
                    prior_growth_score = prior_scores.get(eid),
                )
                growth_score: int          = growth_result["score"]
                band:         str          = growth_result["band"]
                trajectory:   Optional[str] = growth_result["trajectory"]
                components:   Dict[str, Any] = growth_result["components"]

                logger.debug(
                    "[STROMA] Growth: employee=%s score=%d band=%s trajectory=%s",
                    eid, growth_score, band, trajectory,
                )

                # ── Step 5a: Upsert base GrowthSnapshot row ───────────────
                existing_snap = db.execute(
                    select(GrowthSnapshot).where(
                        GrowthSnapshot.employee_id    == eid,
                        GrowthSnapshot.snapshot_month == snapshot_month,
                    )
                ).scalar_one_or_none()

                if existing_snap is None:
                    snap = GrowthSnapshot(
                        employee_id       = eid,
                        snapshot_month    = snapshot_month,
                        growth_score      = growth_score,
                        growth_band       = band,
                        growth_trajectory = trajectory,
                        workforce_health  = {},     # deep-merged below
                    )
                    db.add(snap)
                    db.flush()   # write to DB within transaction (assigns PK)
                else:
                    # Idempotent re-run: update in-place
                    existing_snap.growth_score      = growth_score
                    existing_snap.growth_band       = band
                    existing_snap.growth_trajectory = trajectory
                    db.flush()

                # ── Step 5b: Deep-merge full health blob ──────────────────
                # planner_health is left empty ({}) here; the LangGraph pipeline
                # fills it at query time when it computes per-employee health
                # metrics.  The monthly job owns the STROMA growth components.
                blob = build_workforce_health_blob(
                    health_metrics    = {},          # pipeline fills this later
                    growth_components = components,
                    cell_data         = cell,
                    assessment_data   = {
                        "score": assessment_map.get(eid),
                    },
                )
                upsert_health_into_snapshot(
                    db             = db,
                    employee_id    = eid,
                    snapshot_month = snapshot_month,
                    blob           = blob,
                )

                # ── Step 6: Stage Gate check ──────────────────────────────
                # Skip for outside hires (no lifecycle gate logic applies).
                if not person.outside_hire:
                    gate_flag = check_stage_gate(db, person, today)
                    if gate_flag:
                        results["gate_flags_created"] += 1
                        push_slack_alert(
                            f"[STAGE-GATE] Stage-gate approaching for {person.name} "
                            f"({eid}): stage={gate_flag['from_stage']}, "
                            f"due={gate_flag['gate_due_date']}, "
                            f"days_remaining={gate_flag['days_remaining']}"
                        )

                # ── Step 7: Leech Detection ───────────────────────────────
                leech = check_leech(person, trajectory, growth_score, today)
                if leech:
                    # Only raise a new flag if no open/acknowledged one exists.
                    open_leech = db.execute(
                        select(LeechFlag).where(
                            LeechFlag.employee_id == eid,
                            LeechFlag.status.in_(["open", "acknowledged"]),
                        )
                    ).scalar_one_or_none()

                    if open_leech is None:
                        lf = LeechFlag(
                            employee_id       = eid,
                            months_extended   = leech["months_extended"],
                            growth_trajectory = leech["trajectory"],
                            recommendation    = leech["recommendation"],
                            status            = "open",
                        )
                        db.add(lf)
                        results["leech_flags_created"] += 1
                        push_slack_alert(
                            f"[LEECH] Leech flag raised for {person.name} "
                            f"({eid}): months_extended={leech['months_extended']}, "
                            f"trajectory={leech['trajectory']}, "
                            f"recommendation={leech['recommendation']}"
                        )

                results["employees_processed"] += 1

            except Exception as exc:
                msg = f"[STROMA] Error processing employee={eid}: {exc}"
                logger.exception(msg)
                results["errors"].append(msg)
                # Continue processing remaining employees — a single bad row
                # must not abort the monthly batch for everyone else.
                continue

        # ── Step 8: Audit log ──────────────────────────────────────────────
        audit = STROMAAction(
            action_type  = "monthly_snapshot",
            triggered_by = "schedule",
            payload      = {
                "snapshot_month":      snapshot_month.isoformat(),
                "employees_processed": results["employees_processed"],
                "gate_flags_created":  results["gate_flags_created"],
                "leech_flags_created": results["leech_flags_created"],
                "error_count":         len(results["errors"]),
            },
            status     = "ok" if not results["errors"] else "partial",
            error_text = "; ".join(results["errors"]) or None,
        )
        db.add(audit)
        # session_scope() commits all mutations here on clean exit.

    logger.info(
        "[STROMA] == Monthly job complete: "
        "processed=%d  gates=%d  leeches=%d  errors=%d ==",
        results["employees_processed"],
        results["gate_flags_created"],
        results["leech_flags_created"],
        len(results["errors"]),
    )
    return results


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point  (manual backfills and smoke tests)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s %(levelname)-8s %(name)s - %(message)s",
        datefmt = "%Y-%m-%d %H:%M:%S",
        stream  = sys.stdout,
    )

    ref_date: Optional[date] = None
    if len(sys.argv) > 1:
        try:
            ref_date = date.fromisoformat(sys.argv[1])
            logger.info("[STROMA] Manual run with reference date: %s", ref_date)
        except ValueError:
            logger.error(
                "Invalid date argument %r — expected YYYY-MM-DD format.", sys.argv[1]
            )
            sys.exit(1)

    summary = run_monthly_job(today=ref_date)

    print("\n===================================")
    print("  STROMA Monthly Job -- Summary")
    print("===================================")
    print(json.dumps(summary, indent=2))

    if summary["errors"]:
        print(f"\n[WARNING] {len(summary['errors'])} employee(s) had errors -- check logs.")
        sys.exit(2)