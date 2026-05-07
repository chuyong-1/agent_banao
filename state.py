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
# 5. The Central Graph State
# ---------------------------------------------------------------------------

class GraphState(TypedDict):
    raw_project_input:       str

    extracted_requirements:  Optional[Dict[str, Any]]
    raw_erp_data:            Optional[Dict[str, Any]]
    processed_metrics:       Optional[List[Dict[str, Any]]]
    ranked_candidates:       Optional[List[Dict[str, Any]]]
    disqualified_candidates: Optional[List[Dict[str, Any]]]   # ← NEW
    department_recommendations: Optional[List[Dict[str, Any]]]

    errors: Optional[List[str]]