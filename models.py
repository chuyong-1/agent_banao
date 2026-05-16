"""
models.py — Unified SQLAlchemy Declarative Schema (Phase 1)
=============================================================
Merges the Resource Availability Planner's operational tables with STROMA's
stateful organisational-intelligence tables into a single, cohesive schema
backed by PostgreSQL ≥ 14.

Table tiers
───────────
Tier 1 — Operational (LangGraph pipeline reads these on every request):
    departments, people, assignments, leaves, bounties,
    skill_profiles

Tier 2 — Intelligence snapshots (written by STROMA monthly cron jobs,
    read by the pipeline for scoring context):
    growth_snapshots, capacity_snapshots

Tier 3 — STROMA lifecycle (written and read exclusively by STROMA):
    stage_transitions, stage_gate_flags, assessment_results,
    skill_history, compensation_records, hiring_flags,
    leech_flags, stroma_actions

Design decisions
────────────────
• Every table has a UUID surrogate PK (id). Business-key uniqueness is
  enforced separately (e.g. UNIQUE on employee_id, assignment_id).
• Operational tables reference people.employee_id (TEXT FK) rather than
  people.id (UUID FK). This mirrors STROMA's loose-coupling philosophy and
  matches the string IDs already in use across CELL and the ERP.
• JSONB columns carry a descriptive inline comment on expected structure.
  The application layer owns validation; the DB stores raw JSON for
  flexibility during schema evolution.
• All timestamps are TIMESTAMPTZ (UTC). Date-only columns use DATE.
• Numeric monetary/rate fields use NUMERIC for precision; utilisation/hours
  fields use FLOAT (sub-cent precision not needed there).
• ORM relationships are defined where they add value to query patterns.
  Use `lazy="select"` (default) for all; upgrade to `lazy="joined"` per
  query if N+1 becomes measurable.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


# ─────────────────────────────────────────────────────────────────────────────
# Declarative base
# ─────────────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    """
    Shared declarative base.  All models inherit from this so that
    ``Base.metadata.create_all(engine)`` creates every table at once and
    Alembic's autogenerate sees a single metadata object.
    """
    pass


# ═════════════════════════════════════════════════════════════════════════════
# TIER 1 — OPERATIONAL TABLES
# ═════════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# departments
# ─────────────────────────────────────────────────────────────────────────────

class Department(Base):
    """
    One row per organisational department.

    erp_department_id   Canonical identifier in the upstream ERP / intranet
                        HRMS.  Used as the stable join key when syncing.
    head_employee_id    employee_id of the current department head.
                        Nullable — a department may exist before a head is
                        assigned.  Not a FK so that the dept row can be
                        created before the person row.
    """

    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    erp_department_id: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    head_employee_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Relationships ────────────────────────────────────────────────────────
    people: Mapped[List["Person"]] = relationship(
        "Person", back_populates="department"
    )
    capacity_snapshots: Mapped[List["CapacitySnapshot"]] = relationship(
        "CapacitySnapshot", back_populates="department"
    )
    hiring_flags: Mapped[List["HiringFlag"]] = relationship(
        "HiringFlag", back_populates="department"
    )


# ─────────────────────────────────────────────────────────────────────────────
# people
# ─────────────────────────────────────────────────────────────────────────────

class Person(Base):
    """
    One row per employee or intern from join_date onwards.

    role                Job title as it appears in the intranet HRMS
                        (e.g. "Software Engineer", "Product Analyst").
                        Distinct from current_stage (lifecycle state).
    capacity_hours_per_week
                        Contractual available hours per week.  0.0 means the
                        person is inactive or on a leave of absence and will
                        be hard-disqualified by the pipeline.
    hourly_rate_usd     Billing / cost rate in USD.  Used by the pipeline to
                        produce cost estimates.  For bounty-only interns this
                        is effectively 0.
    current_stage       Lifecycle stage in the STROMA state machine.
                        Constrained to the valid set; changes are logged in
                        stage_transitions.
    stage_start_date    Date the person entered their current stage.  STROMA
                        uses (today − stage_start_date) to decide gate timing,
                        after subtracting approved leave days.
    outside_hire        True for employees hired outside the normal intern
                        pipeline (e.g. a full-time sales hire on day 1).
                        STROMA skips lifecycle gate logic for these people.
    """

    __tablename__ = "people"
    __table_args__ = (
        CheckConstraint(
            "current_stage IN ("
            "'intern_bounty','intern_hybrid','full_time',"
            "'apm','tech_lead','pm','dept_head'"
            ")",
            name="ck_people_current_stage",
        ),
        CheckConstraint(
            "exit_reason IN ("
            "'graduated_let_go','voluntary_exit','converted','extended'"
            ") OR exit_reason IS NULL",
            name="ck_people_exit_reason",
        ),
        # Composite index used by the pipeline to quickly fetch all active
        # people for a given department.
        Index("ix_people_dept_active", "department_id", "active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    employee_id: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slack_user_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    department_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    capacity_hours_per_week: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    # NUMERIC for monetary precision; cast to float in application layer.
    hourly_rate_usd = mapped_column(Numeric(10, 4), default=0.0, nullable=False)
    current_stage: Mapped[str] = mapped_column(String(50), nullable=False)
    join_date: Mapped[date] = mapped_column(Date, nullable=False)
    stage_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True
    )
    exit_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )
    outside_hire: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Relationships ────────────────────────────────────────────────────────
    department: Mapped[Optional["Department"]] = relationship(
        "Department", back_populates="people"
    )
    # Child tables reference people.employee_id (TEXT), not people.id (UUID).
    # foreign_keys must be specified explicitly when the FK target is not the
    # PK of the parent table.
    assignments: Mapped[List["Assignment"]] = relationship(
        "Assignment",
        primaryjoin="Person.employee_id == Assignment.employee_id",
        foreign_keys="[Assignment.employee_id]",
        back_populates="person",
    )
    leaves: Mapped[List["Leave"]] = relationship(
        "Leave",
        primaryjoin="Person.employee_id == Leave.employee_id",
        foreign_keys="[Leave.employee_id]",
        back_populates="person",
    )
    bounties: Mapped[List["Bounty"]] = relationship(
        "Bounty",
        primaryjoin="Person.employee_id == Bounty.employee_id",
        foreign_keys="[Bounty.employee_id]",
        back_populates="person",
    )
    skill_profile: Mapped[Optional["SkillProfile"]] = relationship(
        "SkillProfile",
        primaryjoin="Person.employee_id == SkillProfile.employee_id",
        foreign_keys="[SkillProfile.employee_id]",
        back_populates="person",
        uselist=False,
    )
    growth_snapshots: Mapped[List["GrowthSnapshot"]] = relationship(
        "GrowthSnapshot",
        primaryjoin="Person.employee_id == GrowthSnapshot.employee_id",
        foreign_keys="[GrowthSnapshot.employee_id]",
        back_populates="person",
        order_by="GrowthSnapshot.snapshot_month",
    )


# ─────────────────────────────────────────────────────────────────────────────
# assignments
# ─────────────────────────────────────────────────────────────────────────────

class Assignment(Base):
    """
    A project allocation for one employee.

    hours_per_week      Hours committed to this project per week.  Summed
                        across all active assignments by compute_metrics_node
                        to derive utilisation.
    start_date/end_date Calendar dates.  An assignment is "active" for the
                        pipeline if end_date >= project_start_date.
    """

    __tablename__ = "assignments"
    __table_args__ = (
        Index("ix_assignments_employee_end", "employee_id", "end_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    assignment_id: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    employee_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("people.employee_id", ondelete="CASCADE"),
        nullable=False,
    )
    project_name: Mapped[str] = mapped_column(String(255), nullable=False)
    hours_per_week: Mapped[float] = mapped_column(Float, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    # ── Relationship ─────────────────────────────────────────────────────────
    person: Mapped["Person"] = relationship(
        "Person",
        back_populates="assignments",
        foreign_keys=[employee_id],
    )


# ─────────────────────────────────────────────────────────────────────────────
# leaves
# ─────────────────────────────────────────────────────────────────────────────

class Leave(Base):
    """
    An approved leave record.

    Used by two consumers:
      1. compute_metrics_node / _compute_leave_overlap — overlap penalty.
      2. STROMA stage_gates service — subtracts leave days from active_days
         so the evaluation clock pauses during legitimate absence
         (Time Freezing fix).

    leave_type  One of: PTO | Sick | Parental | Unpaid | Public Holiday
    """

    __tablename__ = "leaves"
    __table_args__ = (
        Index("ix_leaves_employee_dates", "employee_id", "start_date", "end_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    leave_id: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    employee_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("people.employee_id", ondelete="CASCADE"),
        nullable=False,
    )
    leave_type: Mapped[str] = mapped_column(String(50), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    # ── Relationship ─────────────────────────────────────────────────────────
    person: Mapped["Person"] = relationship(
        "Person",
        back_populates="leaves",
        foreign_keys=[employee_id],
    )


# ─────────────────────────────────────────────────────────────────────────────
# bounties
# ─────────────────────────────────────────────────────────────────────────────

class Bounty(Base):
    """
    A discrete task assigned to an employee.

    Bounties serve dual roles:
      • Resource Planner  — reliability_score via _compute_bounty_metrics.
      • STROMA            — growth_score via bounty velocity / percentile.

    status      Must match BountyStatus enum in state.py:
                completed | in_progress | not_started | overdue
    hours_estimated
                Expected effort.  Used by the pipeline to compute an urgency-
                weighted weekly capacity drain for in-progress bounties.
    description Free-text description kept here for CELL sync fidelity.
                Not used in pipeline scoring but surfaced in chatbot context.
    """

    __tablename__ = "bounties"
    __table_args__ = (
        CheckConstraint(
            "status IN ('completed','in_progress','not_started','overdue')",
            name="ck_bounties_status",
        ),
        Index("ix_bounties_employee_status", "employee_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    bounty_id: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    employee_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("people.employee_id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hours_estimated: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    completed_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    # ── Relationship ─────────────────────────────────────────────────────────
    person: Mapped["Person"] = relationship(
        "Person",
        back_populates="bounties",
        foreign_keys=[employee_id],
    )


# ─────────────────────────────────────────────────────────────────────────────
# skill_profiles
# ─────────────────────────────────────────────────────────────────────────────

class SkillProfile(Base):
    """
    Current skill profile for one employee.  One row per person; updated
    monthly by STROMA from assessment results and HRMS write-back.

    skills JSONB structure (array of objects):
        [
          {
            "name":         "Python",
            "category":     "Backend",
            "proficiency":  "strong",   -- beginner | competent | strong
            "last_updated": "2025-04-01"
          },
          ...
        ]

    ingest_erp_data_node maps "proficiency" strings → integers (1–5) before
    handing to the pipeline:
        beginner  → 2
        competent → 3
        strong    → 4
    """

    __tablename__ = "skill_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    employee_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("people.employee_id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    # JSONB: [{name, category, proficiency, last_updated}]
    skills = mapped_column(JSONB, nullable=False, server_default="'[]'::jsonb")
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Relationship ─────────────────────────────────────────────────────────
    person: Mapped["Person"] = relationship(
        "Person",
        back_populates="skill_profile",
        foreign_keys=[employee_id],
    )


# ═════════════════════════════════════════════════════════════════════════════
# TIER 2 — INTELLIGENCE SNAPSHOTS
# ═════════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# growth_snapshots
# ─────────────────────────────────────────────────────────────────────────────

class GrowthSnapshot(Base):
    """
    Monthly growth score snapshot per employee.

    snapshot_month      First calendar day of the month (e.g. 2025-05-01).
                        UNIQUE per (employee_id, snapshot_month) — one record
                        per person per month.
    growth_score        0–100 deterministic score from compute_growth_score().
    growth_band         "strong" | "developing" | "at_risk"
    growth_trajectory   Derived from last 3 snapshots:
                        "improving" | "stable" | "declining"
    workforce_health    Unified JSONB blob that merges the Planner's per-
                        employee health signals with STROMA's growth components.
                        Schema:
                        {
                          "components": {           -- STROMA growth penalties
                            "completion_penalty": int,
                            "carry_penalty":      int,
                            "eod_penalty":        int,
                            "assessment_penalty": int,
                            "velocity_penalty":   int
                          },
                          "cell_data":    {...},    -- raw CELL summary
                          "assessment":  {...},    -- raw assessment data
                          "planner_health": {...}  -- WorkforceHealthMetrics dump
                        }
    """

    __tablename__ = "growth_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "employee_id", "snapshot_month",
            name="uq_growth_snapshots_emp_month",
        ),
        Index("ix_growth_snapshots_emp_month", "employee_id", "snapshot_month"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    employee_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("people.employee_id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_month: Mapped[date] = mapped_column(Date, nullable=False)
    growth_score: Mapped[int] = mapped_column(
        Integer,
        CheckConstraint("growth_score BETWEEN 0 AND 100", name="ck_growth_score_range"),
        nullable=False,
    )
    growth_band: Mapped[str] = mapped_column(
        String(20),
        CheckConstraint(
            "growth_band IN ('strong','developing','at_risk')",
            name="ck_growth_band",
        ),
        nullable=False,
    )
    growth_trajectory: Mapped[Optional[str]] = mapped_column(
        String(20),
        CheckConstraint(
            "growth_trajectory IN ('improving','stable','declining') OR growth_trajectory IS NULL",
            name="ck_growth_trajectory",
        ),
        nullable=True,
    )
    # JSONB: unified workforce_health blob (see docstring above)
    workforce_health = mapped_column(
        JSONB, nullable=False, server_default="'{}'::jsonb"
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Relationship ─────────────────────────────────────────────────────────
    person: Mapped["Person"] = relationship(
        "Person",
        back_populates="growth_snapshots",
        foreign_keys=[employee_id],
    )


# ─────────────────────────────────────────────────────────────────────────────
# capacity_snapshots
# ─────────────────────────────────────────────────────────────────────────────

class CapacitySnapshot(Base):
    """
    Monthly capacity snapshot per department.

    headcount       Active people in this department on snapshot_date.
    bench_metrics   Unified JSONB merging STROMA's by_stage breakdown with the
                    Resource Planner's bench analysis output.
                    Schema:
                    {
                      "by_stage": {                 -- headcount per lifecycle stage
                        "intern_bounty": int,
                        "intern_hybrid": int,
                        "full_time":     int,
                        "apm":           int
                      },
                      "capacity_score": float,      -- headcount / projected_need
                      "bench_summary":  {...}        -- compute_bench_summary() output
                    }
    """

    __tablename__ = "capacity_snapshots"
    __table_args__ = (
        Index("ix_capacity_snapshots_dept_date", "department_id", "snapshot_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    headcount: Mapped[int] = mapped_column(Integer, nullable=False)
    # JSONB: {by_stage, capacity_score, bench_summary}
    bench_metrics = mapped_column(
        JSONB, nullable=False, server_default="'{}'::jsonb"
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── Relationship ─────────────────────────────────────────────────────────
    department: Mapped["Department"] = relationship(
        "Department", back_populates="capacity_snapshots"
    )


# ═════════════════════════════════════════════════════════════════════════════
# TIER 3 — STROMA LIFECYCLE TABLES
# (written and read exclusively by the STROMA monthly pipeline and API layer)
# ═════════════════════════════════════════════════════════════════════════════

# ─────────────────────────────────────────────────────────────────────────────
# stage_transitions  — full audit trail of every gate decision
# ─────────────────────────────────────────────────────────────────────────────

class StageTransition(Base):
    """
    Immutable audit log: one row per gate decision.

    decision    "progress" | "extend" | "let_go" | "convert_to_apm"
    decided_by  employee_id of the approver (dept head or tech lead).
    notes       Free-text rationale; fed to the STROMA chatbot for context.
    """

    __tablename__ = "stage_transitions"
    __table_args__ = (
        Index("ix_stage_transitions_employee", "employee_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    employee_id: Mapped[str] = mapped_column(String(100), nullable=False)
    from_stage: Mapped[str] = mapped_column(String(50), nullable=False)
    to_stage: Mapped[str] = mapped_column(String(50), nullable=False)
    decision: Mapped[str] = mapped_column(
        String(20),
        CheckConstraint(
            "decision IN ('progress','extend','let_go','convert_to_apm')",
            name="ck_stage_transition_decision",
        ),
        nullable=False,
    )
    decided_by: Mapped[str] = mapped_column(String(100), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────────────
# stage_gate_flags  — pending gate decisions that need human action
# ─────────────────────────────────────────────────────────────────────────────

class StageGateFlag(Base):
    """
    Tracks the state of an upcoming gate decision.

    Created by the monthly job when (today + STAGE_GATE_WARNING_DAYS) ≥
    gate_due_date.  Resolved when a StageTransition is recorded for the same
    (employee_id, from_stage) pair.  Escalated if still open after
    STAGE_GATE_ESCALATION_DAYS past gate_due_date.
    """

    __tablename__ = "stage_gate_flags"
    __table_args__ = (
        Index("ix_stage_gate_flags_employee_resolved", "employee_id", "resolved"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    employee_id: Mapped[str] = mapped_column(String(100), nullable=False)
    gate_due_date: Mapped[date] = mapped_column(Date, nullable=False)
    from_stage: Mapped[str] = mapped_column(String(50), nullable=False)
    flag_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    escalated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────────────
# assessment_results  — raw monthly test scores ingested from assessment system
# ─────────────────────────────────────────────────────────────────────────────

class AssessmentResult(Base):
    """
    Raw ingest from the external assessment system via POST /stroma/sync-assessment.

    components JSONB:
        {"technical": int, "reasoning": int, "communication": int, ...}
    """

    __tablename__ = "assessment_results"
    __table_args__ = (
        UniqueConstraint(
            "employee_id", "assessment_date",
            name="uq_assessment_emp_date",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    employee_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    assessment_date: Mapped[date] = mapped_column(Date, nullable=False)
    score: Mapped[int] = mapped_column(
        Integer,
        CheckConstraint("score BETWEEN 0 AND 100", name="ck_assessment_score_range"),
        nullable=False,
    )
    # JSONB: {technical, reasoning, communication, ...}
    components = mapped_column(JSONB, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────────────
# skill_history  — point-in-time snapshots of skill_profiles for delta tracking
# ─────────────────────────────────────────────────────────────────────────────

class SkillHistory(Base):
    """
    Immutable archive row written each time skill_profiles is updated.
    Allows STROMA to compute skill delta over time and surface growth
    trajectory in the chatbot.

    source  "assessment" | "hrms_manual" | "stroma_inferred"
    """

    __tablename__ = "skill_history"
    __table_args__ = (
        Index("ix_skill_history_emp_date", "employee_id", "snapshot_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    employee_id: Mapped[str] = mapped_column(String(100), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    # JSONB: [{name, category, proficiency, last_updated}]
    skills = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(
        String(30),
        CheckConstraint(
            "source IN ('assessment','hrms_manual','stroma_inferred')",
            name="ck_skill_history_source",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────────────
# compensation_records  — pay configuration per employee per stage
# ─────────────────────────────────────────────────────────────────────────────

class CompensationRecord(Base):
    """
    Tracks what an employee is being paid at each lifecycle stage.

    pay_type        "bounty_only"    — intern_bounty stage; cost = units × bounty_rate
                    "fixed_stipend"  — intern_hybrid; fixed INR + tracked bounties
                    "fixed_salary"   — full_time / apm; fixed INR
    fixed_amount    Monthly INR amount.  NULL for bounty_only.
    bounty_rate     INR per bounty unit (default ₹100).  Non-zero even for
                    fixed-pay stages so STROMA can report total compensation.
    """

    __tablename__ = "compensation_records"
    __table_args__ = (
        Index("ix_compensation_emp_from", "employee_id", "effective_from"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    employee_id: Mapped[str] = mapped_column(String(100), nullable=False)
    stage: Mapped[str] = mapped_column(String(50), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    pay_type: Mapped[str] = mapped_column(
        String(20),
        CheckConstraint(
            "pay_type IN ('bounty_only','fixed_stipend','fixed_salary')",
            name="ck_comp_pay_type",
        ),
        nullable=False,
    )
    fixed_amount = mapped_column(Numeric(10, 2), nullable=True)
    bounty_rate = mapped_column(Numeric(5, 2), default=100.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────────────
# hiring_flags  — open hiring recommendations per department
# ─────────────────────────────────────────────────────────────────────────────

class HiringFlag(Base):
    """
    Created by the monthly job when should_flag_hiring() returns a non-null
    result.  Sent to HR + dept head via Slack.  Resolved when HR acknowledges
    or closes the hiring round.

    urgency     "high"   — bench_after < projected_need × 0.5
                "medium" — bench_after < projected_need × 0.7
    """

    __tablename__ = "hiring_flags"
    __table_args__ = (
        Index("ix_hiring_flags_dept_status", "department_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="CASCADE"),
        nullable=False,
    )
    shortfall: Mapped[int] = mapped_column(Integer, nullable=False)
    graduating_soon: Mapped[int] = mapped_column(Integer, nullable=False)
    recommended_batch_size: Mapped[int] = mapped_column(Integer, nullable=False)
    hire_by_date: Mapped[date] = mapped_column(Date, nullable=False)
    urgency: Mapped[str] = mapped_column(
        String(10),
        CheckConstraint(
            "urgency IN ('high','medium','low')", name="ck_hiring_urgency"
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        CheckConstraint(
            "status IN ('open','acknowledged','resolved')",
            name="ck_hiring_status",
        ),
        default="open",
        nullable=False,
    )
    flagged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Relationship ─────────────────────────────────────────────────────────
    department: Mapped["Department"] = relationship(
        "Department", back_populates="hiring_flags"
    )


# ─────────────────────────────────────────────────────────────────────────────
# leech_flags  — extended interns with flat/declining growth
# ─────────────────────────────────────────────────────────────────────────────

class LeechFlag(Base):
    """
    Fired when an intern is past 6 months, has no conversion in progress, and
    shows a flat or declining growth trajectory over the last 2 months.

    recommendation  "let_go"        — strong signal; advise termination
                    "final_warning" — moderate signal; one more month to improve
                    "monitor"       — early signal; watch next snapshot
    """

    __tablename__ = "leech_flags"
    __table_args__ = (
        Index("ix_leech_flags_emp_status", "employee_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    employee_id: Mapped[str] = mapped_column(String(100), nullable=False)
    flagged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    months_extended: Mapped[int] = mapped_column(Integer, nullable=False)
    growth_trajectory: Mapped[str] = mapped_column(String(20), nullable=False)
    recommendation: Mapped[str] = mapped_column(
        String(20),
        CheckConstraint(
            "recommendation IN ('let_go','final_warning','monitor')",
            name="ck_leech_recommendation",
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        CheckConstraint(
            "status IN ('open','acknowledged','resolved')",
            name="ck_leech_status",
        ),
        default="open",
        nullable=False,
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)


# ─────────────────────────────────────────────────────────────────────────────
# stroma_actions  — immutable audit log of every action STROMA takes
# ─────────────────────────────────────────────────────────────────────────────

class StrOMAAction(Base):
    """
    Append-only audit log. One row per significant action taken by any part of
    the STROMA system (cron jobs, API handlers, Slack callbacks).

    action_type     One of:
                    monthly_snapshot | stage_gate_flag | hiring_flag |
                    leech_flag | assessment_ingested | stage_transition |
                    growth_score_computed | slack_alert_sent
    triggered_by    "schedule" | "webhook" | "intranet_api" | "slack_command"
    payload JSONB   Input / output snapshot for the action. Kept for debugging
                    and for the chatbot to explain past decisions.
    """

    __tablename__ = "stroma_actions"
    __table_args__ = (
        Index("ix_stroma_actions_employee_type", "employee_id", "action_type"),
        Index("ix_stroma_actions_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    employee_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    department_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # JSONB: arbitrary input/output snapshot
    payload = mapped_column(JSONB, nullable=True)
    triggered_by: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="ok", nullable=False)
    error_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )