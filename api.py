"""
api.py — STROMA FastAPI Server
==============================
Exposes endpoints for the CELL agent to push data, and for human 
managers (via the HRMS) to pull dashboards and approve transitions.
"""

from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from typing import Dict, List, Optional
from datetime import date
from sqlalchemy import select

from db import session_scope
from models import AssessmentResult, Person, Department, StageTransition, GrowthSnapshot

app = FastAPI(title="STROMA Organizational Intelligence API", version="1.0")

# ── Pydantic Schemas for Requests ──────────────────────────────────────────

class CellSyncPayload(BaseModel):
    employee_id: str
    completion_rate: float
    carry_rate: float
    eod_compliance_rate: float
    bounty_percentile: float

class StageTransitionPayload(BaseModel):
    employee_id: str
    approved_by: str
    new_stage: str
    notes: Optional[str] = None

class SyncAssessmentPayload(BaseModel):
    """Schema for POST /stroma/sync-assessment — raw test score ingestion."""
    employee_id: str
    assessment_date: date
    score: int
    components: Dict[str, int] = {}
    notes: Optional[str] = None

# ── 1. The Ingestion Webhooks (For AI Agents) ──────────────────────────────

@app.post("/stroma/sync-cell", tags=["Ingestion"])
def sync_cell_data(payload: CellSyncPayload):
    """
    Called weekly by the CELL agent to push productivity metrics.
    In a full production environment, this would save to a `cell_raw_metrics` table
    so `monthly_job.py` can aggregate it later.
    """
    # For now, we just acknowledge receipt to unblock the CELL agent
    return {"status": "success", "message": f"Metrics received for {payload.employee_id}"}


@app.post("/stroma/sync-assessment", tags=["Ingestion"])
def sync_assessment(payload: SyncAssessmentPayload):
    """
    Called by the external assessment system to ingest raw test scores
    into the ``assessment_results`` table.

    Upsert semantics: if a row for the same (employee_id, assessment_date)
    already exists, the score / components / notes are updated in-place so
    the endpoint is safely idempotent.
    """
    if not 0 <= payload.score <= 100:
        raise HTTPException(status_code=422, detail="Score must be between 0 and 100")

    with session_scope() as db:
        # Check if the employee actually exists
        person = db.execute(
            select(Person).where(Person.employee_id == payload.employee_id)
        ).scalar_one_or_none()
        if not person:
            raise HTTPException(status_code=404, detail="Employee not found")

        # Upsert: update if (employee_id, assessment_date) already exists
        existing = db.execute(
            select(AssessmentResult).where(
                AssessmentResult.employee_id == payload.employee_id,
                AssessmentResult.assessment_date == payload.assessment_date,
            )
        ).scalar_one_or_none()

        if existing:
            existing.score = payload.score
            existing.components = payload.components
            existing.notes = payload.notes
            action = "updated"
        else:
            ar = AssessmentResult(
                employee_id=payload.employee_id,
                assessment_date=payload.assessment_date,
                score=payload.score,
                components=payload.components,
                notes=payload.notes,
            )
            db.add(ar)
            action = "created"

        return {
            "status": "success",
            "action": action,
            "message": f"Assessment for {person.name} on {payload.assessment_date} {action}",
        }


# ── 2. The Manager API (For Human HRMS) ────────────────────────────────────

@app.post("/stroma/stage-transition", tags=["Management"])
def approve_stage_transition(payload: StageTransitionPayload):
    """
    Called by a Department Head in the HRMS to officially promote an intern
    (e.g., from 'intern_hybrid' to 'full_time').
    """
    with session_scope() as db:
        person = db.execute(select(Person).where(Person.employee_id == payload.employee_id)).scalar_one_or_none()
        
        if not person:
            raise HTTPException(status_code=404, detail="Employee not found")

        # Record the audit trail
        transition = StageTransition(
            employee_id=person.employee_id,
            from_stage=person.current_stage,
            to_stage=payload.new_stage,
            decision="progress",
            decided_by=payload.approved_by,
            effective_date=date.today(),
            notes=payload.notes
        )
        db.add(transition)

        # Update the actual employee record
        person.current_stage = payload.new_stage
        person.stage_start_date = date.today()

        return {"status": "success", "message": f"{person.name} promoted to {payload.new_stage}"}


@app.get("/stroma/department/{erp_department_id}/snapshot", tags=["Dashboards"])
def get_department_snapshot(erp_department_id: str):
    """
    Feeds the UI dashboard for a specific Department Head.
    Returns all active employees and their latest Growth Scores.
    """
    with session_scope() as db:
        dept = db.execute(select(Department).where(Department.erp_department_id == erp_department_id)).scalar_one_or_none()
        if not dept:
            raise HTTPException(status_code=404, detail="Department not found")

        people = db.execute(select(Person).where(Person.department_id == dept.id).where(Person.active.is_(True))).scalars().all()
        
        results = []
        for p in people:
            # Get their most recent growth score
            latest_score = db.execute(
                select(GrowthSnapshot)
                .where(GrowthSnapshot.employee_id == p.employee_id)
                .order_by(GrowthSnapshot.snapshot_month.desc())
            ).scalars().first()

            results.append({
                "employee_id": p.employee_id,
                "name": p.name,
                "role": p.role,
                "stage": p.current_stage,
                "latest_growth_band": latest_score.growth_band if latest_score else "pending",
                "latest_growth_score": latest_score.growth_score if latest_score else None
            })

        return {
            "department": dept.name,
            "headcount": len(results),
            "team_status": results
        }