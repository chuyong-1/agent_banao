"""
state.py — Data Contracts for the Resource Availability Planner
================================================================
v2: Added Bounty tracking, granular leave-overlap metrics,
    reliability scoring, and disqualification logic.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional
from typing_extensions import TypedDict
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
class DepartmentRequirement(BaseModel):
    department: str = Field(
        description="Department / team / stream name for the allocation."
    )
    skills_required: List[str] = Field(
        default_factory=list,
        description="Skills expected for the department allocation."
    )
    people_required: int = Field(default=1, ge=1)


# ---------------------------------------------------------------------------
# 1. LLM Extraction Contract
# ---------------------------------------------------------------------------

class ExtractedRequirements(BaseModel):
    skills_required: List[str] = Field(
        description="Flat list of technical/domain skills required by the project."
    )
    department_requirements: List[DepartmentRequirement] = Field(
        default_factory=list,
        description=(
            "Department-wise skill allotment and headcount requirements, "
            "if provided in the input."
        ),
    )
    department_only: bool = Field(
        default=False,
        description=(
            "If true, only employees from the listed departments should be considered."
        ),
    )
    start_date: str = Field(
        description="ISO-8601 kick-off date. Default 2 weeks from today if missing."
    )
    duration_weeks: int = Field(ge=1)
    hours_per_week: float = Field(ge=1.0, le=60.0)


# ---------------------------------------------------------------------------
# 2. Raw ERP / HR Data Shapes
# ---------------------------------------------------------------------------

class SkillEntry(BaseModel):
    name: str
    proficiency: int = Field(ge=1, le=5)


class Employee(BaseModel):
    id: str
    name: str
    role: str
    department: str
    capacity_hours_per_week: float
    skills: List[SkillEntry]
    hourly_rate_usd: float


class Assignment(BaseModel):
    assignment_id: str
    employee_id: str
    project_name: str
    hours_per_week: float
    start_date: str
    end_date: str


class LeaveRecord(BaseModel):
    leave_id: str
    employee_id: str
    leave_type: str   # "PTO" | "Sick" | "Parental" | "Unpaid"
    start_date: str
    end_date: str


class BountyStatus(str, Enum):
    """
    Lifecycle states for a bounty (a discrete task assigned to an employee).

    completed   — Delivered and accepted. Positive reliability signal.
    in_progress — Currently being worked on. Consumes available hours.
    not_started — Assigned but work hasn't begun. Neutral until past due date.
    overdue     — Past due_date and still not completed. Negative reliability signal.
    """
    COMPLETED   = "completed"
    IN_PROGRESS = "in_progress"
    NOT_STARTED = "not_started"
    OVERDUE     = "overdue"


# ---------------------------------------------------------------------------
# NEW v3: Bench + Health Enumerations
# ---------------------------------------------------------------------------

class BenchStatus(str, Enum):
    """
    Bench allocation state for an employee.

    AVAILABLE_NOW       — Zero active assignment hours; fully on bench.
    PARTIALLY_ALLOCATED — Has work but free capacity ≥ configurable threshold.
    ROLLING_OFF_SOON    — All active assignments end within the rolling-off window.
    FULLY_ALLOCATED     — Active, free capacity below threshold; not overloaded.
    OVERALLOCATED       — Effective utilisation > 100%.
    """
    AVAILABLE_NOW       = "AVAILABLE_NOW"
    PARTIALLY_ALLOCATED = "PARTIALLY_ALLOCATED"
    ROLLING_OFF_SOON    = "ROLLING_OFF_SOON"
    FULLY_ALLOCATED     = "FULLY_ALLOCATED"
    OVERALLOCATED       = "OVERALLOCATED"


class UtilizationBand(str, Enum):
    """
    Coarse utilisation band used by the workforce health engine.

    UNDERUTILIZED    — util ≤ 25%
    HEALTHY          — 25% < util ≤ 75%
    HIGH_UTILIZATION — 75% < util ≤ 90%
    OVERLOADED       — 90% < util ≤ 100%
    CRITICAL         — util > 100%
    """
    UNDERUTILIZED    = "UNDERUTILIZED"
    HEALTHY          = "HEALTHY"
    HIGH_UTILIZATION = "HIGH_UTILIZATION"
    OVERLOADED       = "OVERLOADED"
    CRITICAL         = "CRITICAL"


class BurnoutRisk(str, Enum):
    """Categorical burnout risk derived from burnout_risk_score (0–100)."""
    LOW      = "LOW"       # score ≤ 30
    MODERATE = "MODERATE"  # score ≤ 60
    HIGH     = "HIGH"      # score ≤ 80
    CRITICAL = "CRITICAL"  # score > 80


class Bounty(BaseModel):
    """
    A discrete task (bounty) assigned to an employee.

    hours_estimated : Expected effort to complete this bounty.
                      Used to reduce effective available hours when in_progress.
    due_date        : Deadline. If today > due_date and status != completed
                      → treated as effectively overdue during scoring.
    completed_date  : Set when status == completed (None otherwise).
    """
    bounty_id: str
    employee_id: str
    title: str
    description: str
    hours_estimated: float = Field(ge=0.5)
    status: BountyStatus
    due_date: str           # ISO-8601
    completed_date: Optional[str] = None   # ISO-8601 or None


class ERPData(BaseModel):
    employees:   List[Employee]
    assignments: List[Assignment]
    leaves:      List[LeaveRecord]
    bounties:    List[Bounty]


# ---------------------------------------------------------------------------
# 3. Computed Metrics (output of compute_metrics_node)
# ---------------------------------------------------------------------------

class LeaveOverlapDetail(BaseModel):
    """
    Granular breakdown of how much approved leave overlaps the project window.

    Thresholds used by matchmaker_node:
      overlap_pct >= 100 → is_fully_blocked  → HARD DISQUALIFY
      overlap_pct >=  50 → is_mostly_blocked → SOFT DISQUALIFY (separate list)
      overlap_pct >    0 → partial overlap   → WARNING only
    """
    has_any_overlap:   bool
    overlap_days:      int       # calendar days of leave inside the project window
    project_total_days: int      # total calendar days in the project window
    overlap_pct:       float     # overlap_days / project_total_days × 100
    leave_periods:     List[str] # ["PTO: 2025-06-10 → 2025-06-20"]
    is_fully_blocked:  bool      # overlap_pct >= 100
    is_mostly_blocked: bool      # overlap_pct >= 50


class BountyMetrics(BaseModel):
    """
    Aggregated bounty statistics that feed the reliability_score.

    Reliability Score Formula (0–100):
    ────────────────────────────────────────────────────────────────────────
    base  = (completed / max(1, total_assigned)) × 100
    deduct 15 pts per overdue bounty (hard penalty)
    deduct  5 pts per effectively_overdue bounty (soft penalty)
    bonus  +5 pts if completion_rate > 90 % (consistency bonus)
    reliability_score = clamp(base - penalties + bonus, 0, 100)

    If total_assigned == 0 → reliability_score = 70 (neutral / no history).
    ────────────────────────────────────────────────────────────────────────
    """
    total_assigned: int

    # Counts by status
    completed:           int
    in_progress:         int
    not_started:         int
    overdue:             int      # explicitly marked overdue in the system
    effectively_overdue: int      # not_started or in_progress whose due_date < today

    # Combined negative signal
    total_problematic: int        # overdue + effectively_overdue

    # Hours locked in active (in_progress) bounty work right now.
    # Treated as a weekly deduction: spread over a 2-week window → ÷ 2
    active_bounty_hours:        float   # raw total estimated hours
    active_bounty_hours_weekly: float   # spread across 2 weeks (÷ 2)

    # Core reliability metric (0–100) — see formula above
    reliability_score: float

    # For UI tooltips
    overdue_titles:      List[str]
    in_progress_titles:  List[str]
    completed_titles:    List[str]


class EmployeeMetrics(BaseModel):
    employee_id: str
    name:        str
    role:        str

    # ── Capacity & load ───────────────────────────────────────────────────
    capacity_hours_per_week:   float
    assigned_hours_per_week:   float   # project assignments only
    active_bounty_hours_weekly: float  # in-progress bounty drain (weekly spread)
    effective_assigned_hours:  float   # assignments + bounty drain
    available_hours_per_week:  float   # capacity − effective_assigned (floor 0)
    utilization_pct:           float   # effective_assigned / capacity × 100
    availability_score:        float = Field(ge=0.0, le=100.0)
    projected_free_date:       str

    # ── Overallocation ────────────────────────────────────────────────────
    overallocation_flag: bool          # effective utilisation > 100 %

    # ── Leave ──────────────────────────────────────────────────────────────
    leave_detail: LeaveOverlapDetail

    # ── Bounties ───────────────────────────────────────────────────────────
    bounty_metrics: BountyMetrics

    # ── Pre-computed disqualification ─────────────────────────────────────
    is_disqualified:          bool
    disqualification_reason:  Optional[str]


# ---------------------------------------------------------------------------
# 4. Ranked / Disqualified Candidates (output of matchmaker_node)
# ---------------------------------------------------------------------------

class RankedCandidate(BaseModel):
    rank:            int
    employee_id:     str
    name:            str
    role:            str
    department:      str
    hourly_rate_usd: float

    # ── Composite scores (all 0–100) ──────────────────────────────────────
    fit_score:          float   # 0.45×avail + 0.30×skill + 0.25×reliability
    availability_score: float
    skill_match_score:  float
    reliability_score:  float

    # ── Skill detail ──────────────────────────────────────────────────────
    matched_skills: List[str]
    missing_skills: List[str]

    # ── Availability detail ───────────────────────────────────────────────
    available_hours_per_week: float
    projected_free_date:      str
    overallocation_flag:      bool

    # ── Bounty summary ────────────────────────────────────────────────────
    bounty_summary: Dict[str, Any]

    # ── Leave summary ─────────────────────────────────────────────────────
    leave_overlap_pct:  float
    leave_overlap_days: int

    # ── Human-readable signals ────────────────────────────────────────────
    match_reasons: List[str]
    warnings:      List[str]


class DisqualifiedCandidate(BaseModel):
    """
    Employees hard- or soft-excluded from ranking.
    Surfaced separately in the UI payload so managers see WHY
    someone was excluded — not silently dropped.
    """
    employee_id:             str
    name:                    str
    role:                    str
    department:              str
    disqualification_reason: str
    disqualification_type:   str   # "HARD" or "SOFT"
    leave_overlap_pct:       float
    leave_overlap_days:      int
    overallocation_flag:     bool
    bounty_summary:          Dict[str, Any]


# ---------------------------------------------------------------------------
# NEW v3: Bench Metrics (output of bench.py per-employee computation)
# ---------------------------------------------------------------------------

class BenchMetrics(BaseModel):
    """
    Bench availability status and related metrics for a single employee.

    bench_percentage      : Fraction of weekly capacity that is unallocated (0–100).
    bench_duration_days   : Calendar days until the employee becomes fully benched
                            (0 if already benched).
    projected_bench_date  : ISO-8601 date when the employee is expected to be
                            fully bench-available.
    last_project_end_date : ISO-8601 end date of the most recent active assignment
                            (None if no active assignments).
    primary_skill_cluster : Top skills by proficiency (used for bench skill mapping).
    """
    bench_status:          BenchStatus
    bench_percentage:      float = Field(ge=0.0, le=100.0)
    bench_duration_days:   int   = Field(ge=0)
    projected_bench_date:  str
    last_project_end_date: Optional[str] = None
    primary_skill_cluster: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# NEW v3: Workforce Health Metrics (output of health.py per-employee computation)
# ---------------------------------------------------------------------------

class WorkforceHealthMetrics(BaseModel):
    """
    Workforce health snapshot for a single employee.

    workload_score (0–100)       : Higher = lighter, healthier workload.
    burnout_risk_score (0–100)   : Higher = greater burnout risk.
    sustainability_score (0–100) : Composite of low burnout + healthy load.
    utilization_band             : Coarse utilisation band (UNDERUTILIZED … CRITICAL).
    burnout_risk                 : Categorical risk label.
    health_warnings              : Human-readable warning strings surfaced in UI.
    """
    workload_score:       float = Field(ge=0.0, le=100.0)
    burnout_risk_score:   float = Field(ge=0.0, le=100.0)
    burnout_risk:         BurnoutRisk
    sustainability_score: float = Field(ge=0.0, le=100.0)
    utilization_band:     UtilizationBand
    health_warnings:      List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 5. The Central Graph State
# ---------------------------------------------------------------------------

class GraphState(TypedDict):
    raw_project_input:       str

    extracted_requirements:  Optional[Dict[str, Any]]
    raw_erp_data:            Optional[Dict[str, Any]]
    processed_metrics:       Optional[List[Dict[str, Any]]]
    ranked_candidates:       Optional[List[Dict[str, Any]]]
    disqualified_candidates: Optional[List[Dict[str, Any]]]   # ← v2
    department_recommendations: Optional[List[Dict[str, Any]]]

    # ── NEW v3: Bench + Health aggregate summaries ─────────────────────────
    # Optional so existing callers that do not set them continue to work.
    bench_summary:            Optional[Dict[str, Any]]
    workforce_health_summary: Optional[Dict[str, Any]]

    errors: Optional[List[str]]