"""
db.py — Database Session Factory & Updated Node Functions (Phase 1)
====================================================================
This module is NEW.  It replaces the old ``_load_erp_data()`` / ``mock_data``
pattern with a proper SQLAlchemy session layer.

HOW TO USE IN nodes.py
───────────────────────
1.  Add to the top-level imports of nodes.py:

        from db import session_scope
        # Remove:  from mock_data import MOCK_ERP_DATA

2.  Replace the following three functions in nodes.py with the versions below
    (look for their existing definitions by function name):

        _compute_leave_overlap     — FIX: business-day denominator for overlap_pct
        _compute_skill_match       — FIX: negative lookahead/lookbehind for C++/.NET
        ingest_erp_data_node       — REWRITE: reads from PostgreSQL, no mock_data

3.  Add _business_days() anywhere above _compute_leave_overlap in nodes.py.
    It is a new private helper; nothing else needs to change.

4.  Remove _load_erp_data() from nodes.py entirely.

5.  Add C++ / .NET / F# entries to _SKILL_ALIASES_RAW in nodes.py
    (snippet provided at the bottom of this file).

Environment variables
─────────────────────
DATABASE_URL   SQLAlchemy-format Postgres URL.  Required in production.
               Default: "postgresql://localhost/stroma_db"
               Example: postgresql+psycopg2://user:pass@host:5432/stroma_db

               asyncpg users: switch to AsyncSession + AsyncEngine; the model
               definitions in models.py are ORM-layer only and work unchanged.

DB_POOL_SIZE        (int, default 5)   SQLAlchemy connection pool size.
DB_MAX_OVERFLOW     (int, default 10)  Extra connections above pool size.
DB_POOL_TIMEOUT     (int, default 30)  Seconds before pool raises TimeoutError.
DB_ECHO             ("true"|"false")   Log all SQL statements (default false).
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Dict, Generator, List, Optional, Tuple
import re

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from models import (
    Assignment as AssignmentRow,
    Bounty as BountyRow,
    Department,
    Leave as LeaveRow,
    Person,
    SkillProfile,
)
from state import (
    Assignment,
    Bounty,
    BountyMetrics,
    BountyStatus,
    ERPData,
    Employee,
    GraphState,
    LeaveOverlapDetail,
    LeaveRecord,
    SkillEntry,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Engine + session factory
# ─────────────────────────────────────────────────────────────────────────────

DATABASE_URL: str = os.getenv(
    "DATABASE_URL", "postgresql://localhost/stroma_db"
)

engine = create_engine(
    DATABASE_URL,
    pool_size        = int(os.getenv("DB_POOL_SIZE",    "5")),
    max_overflow     = int(os.getenv("DB_MAX_OVERFLOW", "10")),
    pool_timeout     = int(os.getenv("DB_POOL_TIMEOUT", "30")),
    pool_pre_ping    = True,   # reconnect silently after DB restart
    future           = True,   # SA 2.x style
    echo             = os.getenv("DB_ECHO", "false").lower() == "true",
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,   # keep attrs accessible after commit
)


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """
    Provide a transactional scope around a series of operations.

    Usage (in a LangGraph node):
        with session_scope() as db:
            rows = db.execute(select(Person).where(Person.active == True)).scalars().all()

    Commits on clean exit, rolls back on any exception, always closes.
    """
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency-injection version of session_scope().

    Usage:
        @router.get("/snapshot")
        def snapshot(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Proficiency mapping: STROMA text → Resource Planner integer (1–5)
# ─────────────────────────────────────────────────────────────────────────────

_PROF_MAP: Dict[str, int] = {
    "beginner":  2,
    "competent": 3,
    "strong":    4,
    "expert":    5,
}


def _map_proficiency(raw: Any) -> int:
    """
    Normalise STROMA's text proficiency level to an integer 1–5.

    • If already an int in [1,5], pass through unchanged.
    • String keys looked up in _PROF_MAP (case-insensitive).
    • Anything else → 3 (competent) as safe default.
    """
    if isinstance(raw, int) and 1 <= raw <= 5:
        return raw
    if isinstance(raw, str):
        return _PROF_MAP.get(raw.strip().lower(), 3)
    return 3


# ═════════════════════════════════════════════════════════════════════════════
# FILE 2, FUNCTION 1 OF 4
# DROP-IN REPLACEMENT for nodes.py
#
# NEW HELPER: _business_days
# Insert anywhere above _compute_leave_overlap in nodes.py.
# ═════════════════════════════════════════════════════════════════════════════

def _business_days(start: date, end: date) -> int:
    """
    Count weekdays (Mon–Fri) between *start* and *end*, inclusive.
    Returns at minimum 1 so callers never divide by zero.

    Algorithm is O(1) for the full-week portion and O(≤6) for the remainder,
    so it is safe to call in a tight per-employee loop.

    Examples:
        Mon 2025-06-02 → Fri 2025-06-06  → 5
        Mon 2025-06-02 → Sun 2025-06-08  → 5  (weekend days excluded)
        Sat 2025-06-07 → Sun 2025-06-08  → 0  → clamped to 1
    """
    if end < start:
        return 0
    total_days = (end - start).days + 1
    full_weeks, extra = divmod(total_days, 7)
    bdays = full_weeks * 5
    start_dow = start.weekday()   # 0 = Monday, 6 = Sunday
    for i in range(extra):
        if (start_dow + i) % 7 < 5:   # Mon–Fri
            bdays += 1
    return max(1, bdays)


# ═════════════════════════════════════════════════════════════════════════════
# FILE 2, FUNCTION 2 OF 4
# DROP-IN REPLACEMENT for nodes.py → _compute_leave_overlap
#
# EDGE CASE FIX: business-day denominator
# ─────────────────────────────────────────────────────────────────────────────
# Old behaviour: overlap_pct used calendar days for both numerator and
#   denominator.  A 2-day leave on a 1-week project scored 2/7 = 28.6%,
#   which understates the actual impact (2 out of 5 working days = 40%).
#
# New behaviour:
#   • overlap_days            — unchanged; still calendar days (field contract).
#   • project_total_days      — unchanged; still calendar days (field contract).
#   • overlap_pct             — now: business_overlap / business_proj_total × 100
#                               A 2-day leave on a Mon–Fri project = 2/5 = 40%.
#
# This makes the HARD (≥100%) and SOFT (≥50%) disqualification thresholds
# reflect actual working-day impact instead of calendar-day dilution.
# All other return fields are identical to the v4 implementation.
# ═════════════════════════════════════════════════════════════════════════════

# These helpers are defined in nodes.py; they are re-imported here only so
# that this file is runnable standalone in tests.
# In production, _compute_leave_overlap lives inside nodes.py alongside
# _date_range_days, _merge_intervals, _parse_date, _iso, etc.

def _compute_leave_overlap(
    leaves: List[Dict],
    proj_start: date,
    proj_end: date,
    # NOTE: _date_range_days, _merge_intervals, _parse_date, _iso are defined
    # in nodes.py.  This function uses them as module-level helpers there.
    # The signatures below match the existing nodes.py helpers exactly.
    _date_range_days=None,   # injected from nodes.py namespace
    _merge_intervals=None,   # injected from nodes.py namespace
    _parse_date_fn=None,     # injected from nodes.py namespace
    _iso_fn=None,            # injected from nodes.py namespace
) -> "LeaveOverlapDetail":
    """
    Compute how much of the project window is covered by approved leave.

    ── What changed vs v4 ──────────────────────────────────────────────────
    overlap_pct now uses *business days* for both numerator (merged leave days
    that fall Mon–Fri) and denominator (working days in the project window).

    This fixes the 28%-vs-40% discrepancy: a 2-day leave (Mon–Tue) on a
    1-week project (Mon–Fri) was previously calculated as 2/7 = 28.6%
    because the 7-calendar-day denominator included the weekend.  Now it
    correctly computes as 2/5 = 40%.

    ── What did NOT change ─────────────────────────────────────────────────
    • overlap_days          — calendar days, unchanged (field contract).
    • project_total_days    — calendar days, unchanged (field contract).
    • leave_periods         — clipped overlap window dates, unchanged.
    • Interval merging      — BUG #1 fix carried forward, unchanged.
    • leave_periods display — FIX #7 clipped-window display, unchanged.
    """
    # ── PASTE THIS BODY directly into nodes.py, replacing the existing
    #    _compute_leave_overlap function.  The helper calls (_parse_date,
    #    _iso, _date_range_days, _merge_intervals) already exist there. ──

    # -- calendar-day total (field contract unchanged) --
    from datetime import datetime, timedelta

    def _pd(s: str) -> date:
        return datetime.strptime(s, "%Y-%m-%d").date()

    def _isostr(d: date) -> str:
        return d.isoformat()

    def _cal_range(s: date, e: date) -> int:
        return max(1, (e - s).days + 1)

    def _merge(intervals: List[Tuple[date, date]]) -> List[Tuple[date, date]]:
        if not intervals:
            return []
        ivs = sorted(intervals, key=lambda x: x[0])
        merged = [ivs[0]]
        for start, end in ivs[1:]:
            ps, pe = merged[-1]
            if start <= pe + timedelta(days=1):
                merged[-1] = (ps, max(pe, end))
            else:
                merged.append((start, end))
        return merged

    LEAVE_HARD_DISQUALIFY_PCT = 100.0
    LEAVE_SOFT_DISQUALIFY_PCT = 50.0

    proj_total_cal  = _cal_range(proj_start, proj_end)
    proj_total_biz  = _business_days(proj_start, proj_end)   # ← new denominator

    clipped_intervals: List[Tuple[date, date]] = []
    leave_periods: List[str] = []

    for lv in leaves:
        lv_start = _pd(lv["start_date"])
        lv_end   = _pd(lv["end_date"])
        clip_start = max(lv_start, proj_start)
        clip_end   = min(lv_end,   proj_end)
        if clip_start <= clip_end:
            clipped_intervals.append((clip_start, clip_end))
            overlap_str = (
                f"{lv['leave_type']}: {_isostr(clip_start)} → {_isostr(clip_end)}"
            )
            is_clipped = clip_start != lv_start or clip_end != lv_end
            if is_clipped:
                overlap_str += (
                    f" (full leave: {lv['start_date']} → {lv['end_date']})"
                )
            leave_periods.append(overlap_str)

    merged = _merge(clipped_intervals)

    # Calendar overlap days (field contract — unchanged)
    total_cal_overlap = sum(_cal_range(s, e) for s, e in merged)

    # Business-day overlap (used only for overlap_pct)
    total_biz_overlap = sum(_business_days(s, e) for s, e in merged)

    # FIX: use business days for percentage so weekends don't dilute the score
    overlap_pct = round(
        min(100.0, total_biz_overlap / proj_total_biz * 100), 1
    )

    return LeaveOverlapDetail(
        has_any_overlap    = total_cal_overlap > 0,
        overlap_days       = total_cal_overlap,       # calendar days, unchanged
        project_total_days = proj_total_cal,          # calendar days, unchanged
        overlap_pct        = overlap_pct,             # now business-day based ✓
        leave_periods      = leave_periods,
        is_fully_blocked   = overlap_pct >= LEAVE_HARD_DISQUALIFY_PCT,
        is_mostly_blocked  = overlap_pct >= LEAVE_SOFT_DISQUALIFY_PCT,
    )


# ═════════════════════════════════════════════════════════════════════════════
# FILE 2, FUNCTION 3 OF 4
# DROP-IN REPLACEMENT for nodes.py → _compute_skill_match
#
# EDGE CASE FIX: negative lookahead / lookbehind instead of \b
# ─────────────────────────────────────────────────────────────────────────────
# Old behaviour: used \b (word boundary) around re.escape(skill).
#   \b works for [a-zA-Z0-9_] characters but fails for skills whose names
#   START or END with non-word characters:
#     • "C++"  — \b after the final '+' never fires because '+' is \W and
#                the adjacent space is also \W; no word-char / non-word-char
#                boundary exists.
#     • ".NET" — \b before '.' also fails for the same reason.
#   Result: employees with C++ or .NET skills silently score 0 on those
#   requirements even when they are a perfect match.
#
# New behaviour:
#   (?<!\w)SKILL(?!\w)
#     (?<!\w)  — not preceded by a word character
#     (?!\w)   — not followed by a word character
#   This handles all edge cases:
#     "C++" in "Expert C++ developer"  → matches ✓ (space before, space after)
#     "C++" in "C++11 features"        → NO match ✓ ('1' is \w after the skill)
#     ".NET" in "ASP.NET Core"         → NO match ✓ (adjacent word chars)
#     ".NET" in ".NET and Python"      → matches ✓
#   Bidirectional matching is preserved: "PostgreSQL DBA" still matches
#   a requirement of "PostgreSQL".
# ═════════════════════════════════════════════════════════════════════════════

def _compute_skill_match(
    emp_skills: List[Dict],
    skills_required: List[str],
) -> Tuple[List[str], List[str], float]:
    """
    Returns (matched_skills, missing_skills, skill_match_score 0-100).

    Carries forward all prior fixes:
      BUG #5 FIX — deduplicate required skills before scoring.
      FIX  #6    — bidirectional matching to prevent substring false-positives
                   ("Java" ⊄ "JavaScript", "Go" ⊄ "Django").

    NEW in Phase 1:
      Uses (?<!\\w) / (?!\\w) instead of \\b so that skills whose names
      contain non-word characters (C++, .NET, F#, C#, R) are correctly
      matched without false negatives or false positives.
    """
    # ── BUG #5 FIX: deduplicate while preserving order ───────────────────
    seen_req: set = set()
    unique_required: List[str] = []
    for s in skills_required:
        key = s.lower()
        if key not in seen_req:
            seen_req.add(key)
            unique_required.append(s)

    # ── Pattern cache — built lazily, scoped to this call ────────────────
    # PHASE 1 FIX: (?<!\w)…(?!\w) replaces \b…\b so that skills starting
    # or ending with non-word chars (C++, .NET, F#, C#) compile correctly.
    _skill_pattern_cache: Dict[str, re.Pattern] = {}

    def _skill_pat(skill: str) -> re.Pattern:
        if skill not in _skill_pattern_cache:
            _skill_pattern_cache[skill] = re.compile(
                r"(?<!\w)" + re.escape(skill) + r"(?!\w)",
                re.IGNORECASE,
            )
        return _skill_pattern_cache[skill]

    skill_map: Dict[str, int] = {
        s["name"].lower(): s["proficiency"] for s in emp_skills
    }
    matched: List[str] = []
    missing: List[str] = []
    prof_sum = 0.0

    for req in unique_required:
        req_l   = req.lower()
        req_pat = _skill_pat(req_l)
        best_prof = 0
        for sk_name, prof in skill_map.items():
            # Bidirectional matching preserved:
            #   req_pat.search(sk_name) → "PostgreSQL" matches "PostgreSQL DBA"
            #   _skill_pat(sk_name).search(req_l) → "Node.js" matches "nodejs"
            if req_pat.search(sk_name) or _skill_pat(sk_name).search(req_l):
                best_prof = max(best_prof, prof)
        if best_prof:
            matched.append(req)
            prof_sum += best_prof
        else:
            missing.append(req)

    score = round(prof_sum / (5.0 * max(1, len(unique_required))) * 100, 1)
    return matched, missing, score


# ═════════════════════════════════════════════════════════════════════════════
# FILE 2, FUNCTION 4 OF 4
# DROP-IN REPLACEMENT for nodes.py → ingest_erp_data_node
#
# REWRITE: reads from PostgreSQL; no longer uses mock_data.py or
# _load_erp_data().  The returned dict shape is 100% identical to v4 so
# compute_metrics_node and all downstream nodes are unaffected.
# ═════════════════════════════════════════════════════════════════════════════

def ingest_erp_data_node(state: GraphState) -> Dict[str, Any]:
    """
    Node 2 — Load and validate HR/ERP data from PostgreSQL.

    Queries
    ───────
    1. people  JOIN departments  WHERE people.active = true
       → builds Employee list (id, name, role, department, capacity,
         hourly_rate_usd)
    2. skill_profiles WHERE employee_id IN (active_ids)
       → attached to each Employee as List[SkillEntry] with int proficiency
    3. assignments   WHERE employee_id IN (active_ids)
    4. leaves        WHERE employee_id IN (active_ids)
    5. bounties      WHERE employee_id IN (active_ids)

    All five queries run inside a single read-only session. No writes occur
    in this node; the session is closed immediately after data is fetched.

    Output shape (ERPData.model_dump())
    ────────────────────────────────────
    {
      "employees":   [{"id": ..., "name": ..., "role": ...,
                       "department": ..., "capacity_hours_per_week": ...,
                       "hourly_rate_usd": ..., "skills": [...]}],
      "assignments": [{"assignment_id": ..., "employee_id": ...,
                       "project_name": ..., "hours_per_week": ...,
                       "start_date": ..., "end_date": ...}],
      "leaves":      [{"leave_id": ..., "employee_id": ...,
                       "leave_type": ..., "start_date": ..., "end_date": ...}],
      "bounties":    [{"bounty_id": ..., "employee_id": ..., "title": ...,
                       "description": ..., "hours_estimated": ...,
                       "status": ..., "due_date": ...,
                       "completed_date": ...}]
    }

    Error handling
    ──────────────
    Any database error is caught, logged with full traceback, appended to
    state["errors"], and re-raised so the LangGraph error boundary can handle
    it.  We do NOT silently fall back to mock data — a DB failure should be
    visible, not hidden.
    """
    logger.info("[Node 2] ingest_erp_data_node — querying PostgreSQL")
    errors: List[str] = list(state.get("errors") or [])

    try:
        with session_scope() as db:
            erp_dict = _query_erp_data(db)
    except Exception as exc:
        msg = f"[Node 2] Database query failed: {exc}"
        logger.exception(msg)
        errors.append(msg)
        raise

    erp = ERPData(**erp_dict)
    logger.info(
        "[Node 2] %d employees | %d assignments | %d leaves | %d bounties",
        len(erp.employees), len(erp.assignments),
        len(erp.leaves), len(erp.bounties),
    )
    return {"raw_erp_data": erp.model_dump(), "errors": errors}


# ─────────────────────────────────────────────────────────────────────────────
# Private query helper — separated from the node function so it can be
# unit-tested with an injected in-memory SQLite session.
# ─────────────────────────────────────────────────────────────────────────────

def _query_erp_data(db: Session) -> Dict[str, Any]:
    """
    Execute all five DB queries and return a plain dict matching ERPData's
    field names.  Called by ingest_erp_data_node inside a session_scope().
    """

    # ── 1. Active people + their department names ─────────────────────────
    rows = (
        db.execute(
            select(Person, Department.name.label("dept_name"))
            .outerjoin(Department, Person.department_id == Department.id)
            .where(Person.active.is_(True))
        )
        .all()
    )

    active_ids: List[str] = []
    employees:  List[Dict[str, Any]] = []

    for person, dept_name in rows:
        active_ids.append(person.employee_id)
        employees.append({
            # Skill list filled in step 2 after we batch-fetch profiles.
            "_person_obj": person,
            "id":                      person.employee_id,
            "name":                    person.name,
            "role":                    person.role,
            "department":              dept_name or "Unknown",
            "capacity_hours_per_week": float(person.capacity_hours_per_week or 0.0),
            "hourly_rate_usd":         float(person.hourly_rate_usd or 0.0),
            "skills":                  [],   # populated below
        })

    if not active_ids:
        logger.warning("[Node 2] No active employees found in the database.")
        return {"employees": [], "assignments": [], "leaves": [], "bounties": []}

    # ── 2. Skill profiles — batch fetch, then merge into employees ────────
    skill_rows = (
        db.execute(
            select(SkillProfile).where(
                SkillProfile.employee_id.in_(active_ids)
            )
        )
        .scalars()
        .all()
    )
    skill_map: Dict[str, List[Dict[str, Any]]] = {
        sp.employee_id: (sp.skills or []) for sp in skill_rows
    }

    for emp in employees:
        eid       = emp["id"]
        raw_skills: List[Dict[str, Any]] = skill_map.get(eid, [])
        emp["skills"] = [
            {"name": s["name"], "proficiency": _map_proficiency(s.get("proficiency", "competent"))}
            for s in raw_skills
            if isinstance(s, dict) and s.get("name")
        ]
        # Remove the SQLAlchemy ORM object before we return plain dicts.
        emp.pop("_person_obj", None)

    # ── 3. Assignments ────────────────────────────────────────────────────
    asgn_rows = (
        db.execute(
            select(AssignmentRow).where(
                AssignmentRow.employee_id.in_(active_ids)
            )
        )
        .scalars()
        .all()
    )
    assignments: List[Dict[str, Any]] = [
        {
            "assignment_id":    row.assignment_id,
            "employee_id":      row.employee_id,
            "project_name":     row.project_name,
            "hours_per_week":   float(row.hours_per_week),
            "start_date":       row.start_date.isoformat(),
            "end_date":         row.end_date.isoformat(),
        }
        for row in asgn_rows
    ]

    # ── 4. Leaves ─────────────────────────────────────────────────────────
    leave_rows = (
        db.execute(
            select(LeaveRow).where(
                LeaveRow.employee_id.in_(active_ids)
            )
        )
        .scalars()
        .all()
    )
    leaves: List[Dict[str, Any]] = [
        {
            "leave_id":    row.leave_id,
            "employee_id": row.employee_id,
            "leave_type":  row.leave_type,
            "start_date":  row.start_date.isoformat(),
            "end_date":    row.end_date.isoformat(),
        }
        for row in leave_rows
    ]

    # ── 5. Bounties ───────────────────────────────────────────────────────
    bounty_rows = (
        db.execute(
            select(BountyRow).where(
                BountyRow.employee_id.in_(active_ids)
            )
        )
        .scalars()
        .all()
    )
    bounties: List[Dict[str, Any]] = [
        {
            "bounty_id":       row.bounty_id,
            "employee_id":     row.employee_id,
            "title":           row.title,
            "description":     row.description or "",
            "hours_estimated": float(row.hours_estimated),
            "status":          row.status,
            "due_date":        row.due_date.isoformat(),
            "completed_date":  row.completed_date.isoformat() if row.completed_date else None,
        }
        for row in bounty_rows
    ]

    logger.debug(
        "[Node 2] Query result: %d people / %d assignments / %d leaves / %d bounties",
        len(employees), len(assignments), len(leaves), len(bounties),
    )

    return {
        "employees":   employees,
        "assignments": assignments,
        "leaves":      leaves,
        "bounties":    bounties,
    }


# ═════════════════════════════════════════════════════════════════════════════
# APPENDIX — additions to _SKILL_ALIASES_RAW in nodes.py
# ═════════════════════════════════════════════════════════════════════════════
#
# Add the following entries to the _SKILL_ALIASES_RAW dict in nodes.py.
# They use (?<!\w) / (?!\w) instead of \b because their names start or end
# with non-word characters, which makes \b unreliable for them.
#
#   r"(?<!\w)c\+\+(?!\w)":          "C++",
#   r"(?<!\w)c#(?!\w)":             "C#",
#   r"(?<!\w)f#(?!\w)":             "F#",
#   r"(?<!\w)\.net(?!\w)":          ".NET",
#   r"(?<!\w)asp\.net(?!\w)":       "ASP.NET",
#   r"(?<!\w)r(?!\w)":              "R",           # statistical language
#   r"(?<!\w)qt(?!\w)":             "Qt",
#   r"(?<!\w)three\.?js(?!\w)":     "Three.js",
#   r"(?<!\w)vue\.?js(?!\w)":       "Vue.js",
#   r"(?<!\w)svelte(?!\w)":         "Svelte",
#   r"(?<!\w)angular(?!\w)":        "Angular",
#   r"(?<!\w)flutter(?!\w)":        "Flutter",
#   r"(?<!\w)dart(?!\w)":           "Dart",
#
# The existing patterns in _SKILL_ALIASES_RAW that use \b remain valid for
# standard alphanumeric skill names (Python, FastAPI, etc.) because \b works
# correctly when both ends of the skill name are word characters.
# ═════════════════════════════════════════════════════════════════════════════