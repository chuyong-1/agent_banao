"""
nodes.py — LangGraph Node Functions  (v3 — All Bugs Fixed)
===========================================================
Bug fixes applied:
  #1  _compute_leave_overlap: merge overlapping intervals before summing
      (prevents double-counting when two leave records overlap each other)
  #2  compute_metrics_node: capacity=0 → hard disqualify immediately
      (previously returned availability_score=100 and slipped into ranking)
  #3  _compute_bounty_metrics: reliability denominator now uses only
      "actionable" bounties (completed + problematic), NOT future not_started
      (prevents a single future task from collapsing reliability to 0)
  #4  _heuristic_extract: unknown-skill fallback now extracts UPPERCASE tokens
      or uses a descriptive label instead of always defaulting to "Python"
  #5  _compute_skill_match: deduplicate skills_required before scoring
      (prevents inflated score when the same skill appears twice in the list)
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from state import (
    BountyMetrics,
    BountyStatus,
    DisqualifiedCandidate,
    ERPData,
    EmployeeMetrics,
    ExtractedRequirements,
    GraphState,
    LeaveOverlapDetail,
    RankedCandidate,
)
from mock_data import MOCK_ERP_DATA

logger = logging.getLogger(__name__)


SKILL_ALIASES: Dict[str, str] = {
    r"\bpython\b": "Python",            r"\bfastapi\b": "FastAPI",
    r"\bdjango\b": "Django",             r"\breact\b": "React",
    r"\bnext\.?js\b": "Next.js",         r"\btypescript\b": "TypeScript",
    r"\bgraphql\b": "GraphQL",           r"\bpostgres(?:ql)?\b": "PostgreSQL",
    r"\bmysql\b": "MySQL",               r"\bmongo(?:db)?\b": "MongoDB",
    r"\baws\b": "AWS",                   r"\bazure\b": "Azure",
    r"\bgcp\b|google cloud": "GCP",      r"\bdocker\b": "Docker",
    r"\bkubernetes\b|k8s": "Kubernetes", r"\bterraform\b": "Terraform",
    r"\bkafka\b": "Kafka",               r"\bspark\b": "Apache Spark",
    r"\bdbt\b": "dbt",                   r"\bairflow\b": "Airflow",
    r"\bpytorch\b": "PyTorch",           r"\btensorflow\b": "TensorFlow",
    r"\blangchain\b": "LangChain",
    r"\bml\b|machine learning": "Machine Learning",
    r"\bci[/\-]?cd\b": "CI/CD",          r"\bredis\b": "Redis",
    r"\bprometheus\b": "Prometheus",     r"\bcobol\b": "COBOL",
    r"\bjava\b": "Java",                 r"\brust\b": "Rust",
    r"\bgo(?:lang)?\b": "Go",            r"\bnode\.?js\b|\bnodejs\b": "Node.js",
    r"\bruby\b": "Ruby",                 r"\bscala\b": "Scala",
    r"\belixir\b": "Elixir",
    r"\bmern\b": "MERN Stack",           r"\breact\s+native\b": "React Native",
    r"\bmanual testing\b": "Manual Testing",
    r"\bdigital marketing\b": "Digital Marketing",
    r"\bgraphic design(?:ing)?\b": "Graphic Designing",
    r"\bbusiness analysis\b": "Business Analysis",
    r"\bhuman resources\b|\bhr\b": "Human Resources",
    r"\baccounting\b": "Accounting",
    r"\bui\s*\/\s*ux\b|\bui\s+ux\b": "UI/UX",
    r"\bnetworking automation\b": "Networking Automation",
}

COMMON_TITLE_WORDS = {
    "We", "The", "Our", "This", "That", "Need", "For", "And", "With",
    "From", "Into", "Using", "Start", "Have", "Been", "Will", "Should",
    "Must", "Also", "Some", "Get", "Set", "New", "Build", "Run", "Add",
    "Very", "Good", "Strong", "Ideal", "Lead", "Team", "Work", "Full",
    "Time", "Week", "Hour", "Day", "Year", "Month", "Project", "Task",
    "Senior", "Junior", "Mid", "Level", "Engineer", "Developer", "Analyst",
}


# ═══════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════════

def _today() -> date:
    return date.today()

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def _iso(d: date) -> str:
    return d.isoformat()

def _date_range_days(start: date, end: date) -> int:
    """Inclusive calendar-day count (minimum 1)."""
    return max(1, (end - start).days + 1)


def _dedupe_case_insensitive(items: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for item in items:
        cleaned = item.strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def _extract_skills_from_text(text: str, fallback_unspecified: bool = True) -> List[str]:
    lowered = text.lower()
    skills = [canon for pat, canon in SKILL_ALIASES.items() if re.search(pat, lowered)]
    skills = _dedupe_case_insensitive(skills)

    if not skills:
        all_caps   = re.findall(r"\b[A-Z]{2,}\b", text)
        title_case = re.findall(r"\b[A-Z][a-z]{2,}(?:\.[a-zA-Z]+)?\b", text)
        candidates = [t for t in all_caps + title_case if t not in COMMON_TITLE_WORDS]
        skills = _dedupe_case_insensitive(candidates)

    if not skills and fallback_unspecified:
        skills = ["Unspecified Technical Skill"]

    return skills


def _is_noise_line(line: str) -> bool:
    lowered = line.lower().strip()
    if not lowered:
        return True
    if "private conversation" in lowered:
        return True
    if re.search(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b", lowered):
        return True
    if "yesterday" in lowered and "at" in lowered:
        return True
    if lowered.strip("[]") == "":
        return True
    return False


def _strip_bullet(line: str) -> str:
    return re.sub(r"^[\s\-\*]+", "", line).strip()


def _extract_headcount(line: str) -> Tuple[int, str]:
    patterns = [
        r"(?<!\w)(\d+)\s*(?:people|persons|headcount|hc|resources|staff|members|devs|developers|engineers|testers|designers|analysts)\b",
        r"(?<!\w)(\d+)\s*x\b",
        r"\bx\s*(\d+)\b",
    ]
    for pat in patterns:
        m = re.search(pat, line.lower())
        if m:
            count = max(1, int(m.group(1)))
            cleaned = (line[:m.start()] + line[m.end():]).strip()
            return count, cleaned
    return 1, line


def _clean_department_name(text: str) -> str:
    cleaned = re.sub(r"\b(people|persons|headcount|hc|resources|staff|members|required|needed)\b", "", text, flags=re.I)
    cleaned = re.sub(r"[\-:|]+", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def _parse_department_requirements(text: str) -> Tuple[List[Dict[str, Any]], str]:
    raw_lines = text.splitlines()
    if not raw_lines:
        return [], text

    lines = [ln.strip() for ln in raw_lines if ln.strip()]
    if not lines:
        return [], text

    header_idx = None
    for i, ln in enumerate(lines):
        if re.search(r"\bdepartment(?:\s|-)?wise\b|\bdept(?:\s|-)?wise\b", ln.lower()):
            header_idx = i
            break

    candidate_lines = lines[header_idx + 1:] if header_idx is not None else lines
    candidate_lines = [ln for ln in candidate_lines if not _is_noise_line(ln)]

    if len(candidate_lines) == 1 and re.search(r"[,|]", candidate_lines[0]):
        line_low = candidate_lines[0].lower()
        if re.search(r"\bdepartment\b|\bdept\b|\bteam\b|\bheadcount\b", line_low):
            line = candidate_lines[0]
            if ":" in line and re.search(r"\bdepartment\b|\bdept\b", line.split(":", 1)[0].lower()):
                line = line.split(":", 1)[1]
            candidate_lines = [seg.strip() for seg in re.split(r"[,|]", line) if seg.strip()]

    if header_idx is None:
        if len(candidate_lines) < 3:
            return [], text
        shortish = sum(1 for ln in candidate_lines if len(ln) <= 40)
        if shortish < len(candidate_lines):
            return [], text

    if not candidate_lines:
        return [], text

    dept_map: Dict[str, Dict[str, Any]] = {}
    for raw in candidate_lines:
        line = _strip_bullet(raw)
        if not line or _is_noise_line(line):
            continue
        people_required, line_wo_count = _extract_headcount(line)
        dept_name = _clean_department_name(line_wo_count)
        if not dept_name:
            continue
        skills = _extract_skills_from_text(line, fallback_unspecified=False)

        key = dept_name.lower()
        if key in dept_map:
            dept_map[key]["people_required"] += people_required
            dept_map[key]["skills_required"] = _dedupe_case_insensitive(
                dept_map[key]["skills_required"] + skills
            )
        else:
            dept_map[key] = {
                "department": dept_name,
                "skills_required": _dedupe_case_insensitive(skills),
                "people_required": people_required,
            }

    remainder_lines = lines[:header_idx] if header_idx is not None else []
    remainder_text = "\n".join(remainder_lines).strip()

    return list(dept_map.values()), remainder_text


# ═══════════════════════════════════════════════════════════════════════════
# NODE 1 — extract_intent_node
# ═══════════════════════════════════════════════════════════════════════════

def _llm_extract(text: str) -> ExtractedRequirements:
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import ChatPromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    structured_llm = llm.with_structured_output(ExtractedRequirements)
    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "You are a project intake analyst. Extract resourcing requirements. "
         "If the input includes department-wise allocations, populate "
         "department_requirements with department name, skills_required, "
         "and people_required. "
         "If the input says only those departments should be used, set "
         "department_only = true. "
         "If start_date is missing default to 2 weeks from today ({today}). "
         "If duration is missing default to 8 weeks. "
         "If hours_per_week is missing default to 20."),
        ("human", "{text}"),
    ])
    return (prompt | structured_llm).invoke({"text": text, "today": _iso(_today())})


def _heuristic_extract(text: str) -> ExtractedRequirements:
    """
    Regex + keyword fallback.

    BUG #4 FIX — unknown-skill handling:
      Old: always fell back to ["Python"] when no known skill matched.
      New: extract SCREAMING_CASE or TitleCase tokens that look like tech names.
           If still nothing found, use the literal phrase "Unspecified Technical Skill"
           so skill_match correctly scores 0 for everyone rather than
           artificially matching Python experts.
    """
    lowered = text.lower()

    dept_reqs, remainder_text = _parse_department_requirements(text)
    department_only = bool(
        re.search(
            r"\bdepartment\s+only\b|\bonly\s+department\b|\bdepartment[-\s]?only\b"
            r"|\bsame\s+department\s+only\b|\bstrict\s+department\b",
            lowered,
        )
    )
    skill_source = remainder_text if dept_reqs else text
    skills = _extract_skills_from_text(skill_source, fallback_unspecified=True)

    if dept_reqs:
        dept_skills: List[str] = []
        for d in dept_reqs:
            dept_skills.extend(d.get("skills_required", []))
        dept_skills = _dedupe_case_insensitive(dept_skills)
        if dept_skills:
            if skills == ["Unspecified Technical Skill"]:
                skills = dept_skills
            else:
                skills = _dedupe_case_insensitive(skills + dept_skills)

    # ── Start date ────────────────────────────────────────────────────────
    start_date: date = _today() + timedelta(weeks=2)
    for pat in [r"start(?:ing|s)?\s+(?:on\s+)?(\d{4}-\d{2}-\d{2})",
                r"from\s+(\d{4}-\d{2}-\d{2})",
                r"kick.?off\s+(\d{4}-\d{2}-\d{2})",
                r"begin(?:ning)?\s+(\d{4}-\d{2}-\d{2})"]:
        m = re.search(pat, lowered)
        if m:
            try:
                start_date = _parse_date(m.group(1)); break
            except ValueError:
                pass

    # ── Duration ──────────────────────────────────────────────────────────
    duration_weeks = 8
    for pat, mult in [(r"(\d+)\s*week", 1), (r"(\d+)\s*month", 4), (r"(\d+)\s*sprint", 2)]:
        m = re.search(pat, lowered)
        if m:
            duration_weeks = int(m.group(1)) * mult; break

    # ── Hours per week ────────────────────────────────────────────────────
    hours_per_week = 20.0
    for pat in [r"(\d+)\s*h(?:ou)?rs?\s*(?:per|a|\/)\s*week",
                r"(\d+)\s*h(?:ou)?rs?\s*weekly",
                r"full.?time", r"half.?time"]:
        m = re.search(pat, lowered)
        if m:
            hours_per_week = (40.0 if "full" in (m.group(0) if m.lastindex == 0 else pat)
                              else 20.0) if not m.lastindex else float(m.group(1))
            break

    return ExtractedRequirements(
        skills_required=skills,
        department_requirements=dept_reqs,
        department_only=department_only,
        start_date=_iso(start_date),
        duration_weeks=max(1, duration_weeks),
        hours_per_week=min(60.0, max(1.0, hours_per_week)),
    )


def extract_intent_node(state: GraphState) -> Dict[str, Any]:
    """Node 1 — Parse raw project description → structured requirements."""
    logger.info("[Node 1] extract_intent_node — starting")
    raw_text = state["raw_project_input"]
    errors: List[str] = list(state.get("errors") or [])

    if os.getenv("OPENAI_API_KEY"):
        try:
            reqs = _llm_extract(raw_text)
        except Exception as exc:
            logger.warning("[Node 1] LLM failed (%s), falling back", exc)
            errors.append(f"LLM extraction failed ({exc}); used heuristic fallback.")
            reqs = _heuristic_extract(raw_text)
    else:
        reqs = _heuristic_extract(raw_text)

    logger.info("[Node 1] Extracted: %s", reqs.model_dump())
    return {"extracted_requirements": reqs.model_dump(), "errors": errors}


# ═══════════════════════════════════════════════════════════════════════════
# NODE 2 — ingest_erp_data_node
# ═══════════════════════════════════════════════════════════════════════════

def ingest_erp_data_node(state: GraphState) -> Dict[str, Any]:
    """Node 2 — Load and validate HR/ERP data (mock or real)."""
    logger.info("[Node 2] ingest_erp_data_node — loading data")
    erp = ERPData(**MOCK_ERP_DATA)
    logger.info("[Node 2] %d employees | %d assignments | %d leaves | %d bounties",
                len(erp.employees), len(erp.assignments),
                len(erp.leaves), len(erp.bounties))
    return {"raw_erp_data": erp.model_dump()}


# ═══════════════════════════════════════════════════════════════════════════
# NODE 3 — compute_metrics_node
# ═══════════════════════════════════════════════════════════════════════════

LEAVE_HARD_DISQUALIFY_PCT = 100.0
LEAVE_SOFT_DISQUALIFY_PCT =  50.0


def _merge_intervals(intervals: List[Tuple[date, date]]) -> List[Tuple[date, date]]:
    """
    Merge a list of (start, end) date pairs into non-overlapping intervals.
    Used to prevent double-counting when multiple leave records overlap.

    Example:
      [(Jun 1, Jun 10), (Jun 5, Jun 15)]  →  [(Jun 1, Jun 15)]
    """
    if not intervals:
        return []
    sorted_ivs = sorted(intervals, key=lambda x: x[0])
    merged = [sorted_ivs[0]]
    for start, end in sorted_ivs[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + timedelta(days=1):   # adjacent or overlapping
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _compute_leave_overlap(
    leaves: List[Dict],
    employee_id: str,
    proj_start: date,
    proj_end: date,
) -> LeaveOverlapDetail:
    """
    Compute how much of the project window is covered by approved leave.

    BUG #1 FIX — interval merging:
      Old: summed raw overlap per record → double-counted overlapping leaves.
      New: clips each leave to the project window, merges overlapping clips,
           then sums only the merged intervals.

    overlap_pct is capped at 100 (even if merged intervals exceed the window,
    which can't actually happen after clipping).
    """
    proj_total_days = _date_range_days(proj_start, proj_end)
    clipped_intervals: List[Tuple[date, date]] = []
    leave_periods: List[str] = []

    for lv in leaves:
        if lv["employee_id"] != employee_id:
            continue
        lv_start = _parse_date(lv["start_date"])
        lv_end   = _parse_date(lv["end_date"])

        # Clip to project window
        clip_start = max(lv_start, proj_start)
        clip_end   = min(lv_end,   proj_end)
        if clip_start <= clip_end:
            clipped_intervals.append((clip_start, clip_end))
            leave_periods.append(
                f"{lv['leave_type']}: {lv['start_date']} → {lv['end_date']}"
            )

    # Merge overlapping clips to avoid double-counting (BUG #1 FIX)
    merged = _merge_intervals(clipped_intervals)
    total_overlap = sum(_date_range_days(s, e) for s, e in merged)

    overlap_pct = round(min(100.0, total_overlap / proj_total_days * 100), 1)

    return LeaveOverlapDetail(
        has_any_overlap    = total_overlap > 0,
        overlap_days       = total_overlap,
        project_total_days = proj_total_days,
        overlap_pct        = overlap_pct,
        leave_periods      = leave_periods,
        is_fully_blocked   = overlap_pct >= LEAVE_HARD_DISQUALIFY_PCT,
        is_mostly_blocked  = overlap_pct >= LEAVE_SOFT_DISQUALIFY_PCT,
    )


def _compute_bounty_metrics(
    bounties: List[Dict],
    employee_id: str,
    today: date,
) -> BountyMetrics:
    """
    Aggregate bounty statistics and derive reliability_score (0-100).

    BUG #3 FIX — reliability denominator:
      Old: base = completed / total × 100
           Problem: a single future not_started task collapses score to 0
           even though the employee has done nothing wrong.
      New: denominator = completed + problematic (overdue + eff_overdue)
           Future not_started and in-progress (not yet late) tasks are
           excluded from the base calculation.
           If no completed or problematic bounties → score = 70 (neutral).

    Formula:
      actionable    = completed + total_problematic
      base          = (completed / max(1, actionable)) × 100
      penalty       = min(base, 15 × overdue + 5 × eff_overdue)
      consistency   = +5 if base > 90 % and actionable >= 3
      reliability   = clamp(base − penalty + consistency, 0, 100)
      no history OR only future tasks → 70 (neutral)
    """
    emp_bounties = [b for b in bounties if b["employee_id"] == employee_id]

    if not emp_bounties:
        return BountyMetrics(
            total_assigned=0,
            completed=0, in_progress=0, not_started=0,
            overdue=0, effectively_overdue=0, total_problematic=0,
            active_bounty_hours=0.0, active_bounty_hours_weekly=0.0,
            reliability_score=70.0,
            overdue_titles=[], in_progress_titles=[], completed_titles=[],
        )

    completed_cnt = in_progress_cnt = not_started_cnt = overdue_cnt = 0
    eff_overdue_cnt = 0
    active_hours = 0.0
    overdue_titles: List[str]     = []
    in_progress_titles: List[str] = []
    completed_titles: List[str]   = []

    for b in emp_bounties:
        status      = b["status"]
        due         = _parse_date(b["due_date"])
        title       = b["title"]
        is_past_due = due < today and status != BountyStatus.COMPLETED

        if status == BountyStatus.COMPLETED:
            completed_cnt += 1
            completed_titles.append(title)

        elif status == BountyStatus.IN_PROGRESS:
            in_progress_cnt += 1
            in_progress_titles.append(title)
            active_hours += b["hours_estimated"]
            if is_past_due:
                eff_overdue_cnt += 1   # running but already past deadline

        elif status == BountyStatus.NOT_STARTED:
            not_started_cnt += 1
            if is_past_due:
                eff_overdue_cnt += 1   # never started and already late

        elif status == BountyStatus.OVERDUE:
            overdue_cnt += 1
            overdue_titles.append(title)

    total            = len(emp_bounties)
    total_problematic = overdue_cnt + eff_overdue_cnt

    # BUG #3 FIX: use "actionable" denominator instead of total
    actionable = completed_cnt + total_problematic
    if actionable == 0:
        # Only future/neutral tasks — no track record yet → neutral
        reliability = 70.0
    else:
        base              = (completed_cnt / actionable) * 100
        penalty           = min(base, 15 * overdue_cnt + 5 * eff_overdue_cnt)
        consistency_bonus = 5.0 if (base > 90 and actionable >= 3) else 0.0
        reliability       = round(max(0.0, min(100.0, base - penalty + consistency_bonus)), 1)

    active_hours_weekly = round(active_hours / 2.0, 2)

    return BountyMetrics(
        total_assigned         = total,
        completed              = completed_cnt,
        in_progress            = in_progress_cnt,
        not_started            = not_started_cnt,
        overdue                = overdue_cnt,
        effectively_overdue    = eff_overdue_cnt,
        total_problematic      = total_problematic,
        active_bounty_hours        = active_hours,
        active_bounty_hours_weekly = active_hours_weekly,
        reliability_score      = reliability,
        overdue_titles         = overdue_titles,
        in_progress_titles     = in_progress_titles,
        completed_titles       = completed_titles,
    )


def compute_metrics_node(state: GraphState) -> Dict[str, Any]:
    """
    Node 3 — Derive all per-employee metrics.

    BUG #2 FIX — zero-capacity employees:
      Old: capacity=0 → utilization=0 → availability_score=100 (wrong).
      New: capacity=0 → immediate HARD disqualify with clear reason.
    """
    logger.info("[Node 3] compute_metrics_node — calculating metrics")
    erp  = state["raw_erp_data"]           # type: ignore
    reqs = state["extracted_requirements"] # type: ignore

    proj_start = _parse_date(reqs["start_date"])
    proj_end   = proj_start + timedelta(weeks=reqs["duration_weeks"])
    today      = _today()

    employees   = erp["employees"]
    assignments = erp["assignments"]
    leaves      = erp["leaves"]
    bounties    = erp["bounties"]

    # Index active assignments (those still running when the project starts)
    active_asgns: Dict[str, List[Dict]] = {e["id"]: [] for e in employees}
    for a in assignments:
        if _parse_date(a["end_date"]) >= proj_start:
            active_asgns[a["employee_id"]].append(a)

    metrics: List[Dict[str, Any]] = []

    for emp in employees:
        eid      = emp["id"]
        capacity = emp["capacity_hours_per_week"]

        # ── BUG #2 FIX: zero-capacity guard ───────────────────────────────
        if capacity <= 0:
            # Still compute bounty metrics for the disqualified card
            bm = _compute_bounty_metrics(bounties, eid, today)
            ld = _compute_leave_overlap(leaves, eid, proj_start, proj_end)
            m  = EmployeeMetrics(
                employee_id                = eid,
                name                       = emp["name"],
                role                       = emp["role"],
                capacity_hours_per_week    = 0.0,
                assigned_hours_per_week    = 0.0,
                active_bounty_hours_weekly = 0.0,
                effective_assigned_hours   = 0.0,
                available_hours_per_week   = 0.0,
                utilization_pct            = 0.0,
                availability_score         = 0.0,
                projected_free_date        = _iso(today),
                overallocation_flag        = False,
                leave_detail               = ld,
                bounty_metrics             = bm,
                is_disqualified            = True,
                disqualification_reason    = (
                    "HARD: Employee has zero contractual capacity "
                    "(inactive / on leave of absence)."
                ),
            )
            metrics.append(m.model_dump())
            logger.debug("[Node 3] %s → HARD disq (zero capacity)", emp["name"])
            continue

        # ── Assignment load ────────────────────────────────────────────────
        asgn_hours = sum(a["hours_per_week"] for a in active_asgns.get(eid, []))

        # ── Bounty metrics ─────────────────────────────────────────────────
        bm = _compute_bounty_metrics(bounties, eid, today)

        # ── Effective load = assignments + in-progress bounty drain ────────
        effective_assigned = asgn_hours + bm.active_bounty_hours_weekly
        available_hours    = max(0.0, capacity - effective_assigned)
        utilization_pct    = round(effective_assigned / capacity * 100, 1)
        availability_score = round(max(0.0, min(100.0, 100.0 - utilization_pct)), 1)
        overalloc          = utilization_pct > 100.0

        # ── Projected free date ────────────────────────────────────────────
        emp_asgns = active_asgns.get(eid, [])
        if emp_asgns:
            latest_end          = max(_parse_date(a["end_date"]) for a in emp_asgns)
            projected_free_date = _iso(latest_end + timedelta(days=1))
        else:
            projected_free_date = _iso(today)

        # ── Leave overlap ──────────────────────────────────────────────────
        ld = _compute_leave_overlap(leaves, eid, proj_start, proj_end)

        # ── Disqualification ──────────────────────────────────────────────
        disqualified   = False
        disq_reason: Optional[str] = None

        if ld.is_fully_blocked:
            disqualified = True
            disq_reason  = (
                f"HARD: Leave covers 100% of the project window "
                f"({ld.overlap_days}/{ld.project_total_days} days). "
                f"Periods: {'; '.join(ld.leave_periods)}"
            )
        elif overalloc and available_hours <= 0:
            disqualified = True
            disq_reason  = (
                f"HARD: Over-allocated ({utilization_pct:.0f}% utilisation) "
                "with zero available hours."
            )
        elif ld.is_mostly_blocked:
            disqualified = True
            disq_reason  = (
                f"SOFT: Leave covers {ld.overlap_pct:.0f}% of the project window "
                f"({ld.overlap_days} days). "
                f"Periods: {'; '.join(ld.leave_periods)}"
            )

        m = EmployeeMetrics(
            employee_id                = eid,
            name                       = emp["name"],
            role                       = emp["role"],
            capacity_hours_per_week    = capacity,
            assigned_hours_per_week    = asgn_hours,
            active_bounty_hours_weekly = bm.active_bounty_hours_weekly,
            effective_assigned_hours   = round(effective_assigned, 2),
            available_hours_per_week   = round(available_hours, 2),
            utilization_pct            = utilization_pct,
            availability_score         = availability_score,
            projected_free_date        = projected_free_date,
            overallocation_flag        = overalloc,
            leave_detail               = ld,
            bounty_metrics             = bm,
            is_disqualified            = disqualified,
            disqualification_reason    = disq_reason,
        )
        metrics.append(m.model_dump())
        logger.debug(
            "[Node 3] %-20s util=%.0f%%  avail=%.0f  rely=%.0f  disq=%s",
            emp["name"], utilization_pct, availability_score,
            bm.reliability_score, disq_reason[:50] if disq_reason else "—",
        )

    logger.info("[Node 3] Metrics for %d employees", len(metrics))
    return {"processed_metrics": metrics}


# ═══════════════════════════════════════════════════════════════════════════
# NODE 4 — matchmaker_node
# ═══════════════════════════════════════════════════════════════════════════

W_AVAILABILITY = 0.45
W_SKILL        = 0.30
W_RELIABILITY  = 0.25


def _compute_skill_match(
    emp_skills: List[Dict],
    skills_required: List[str],
) -> Tuple[List[str], List[str], float]:
    """
    Returns (matched_skills, missing_skills, skill_match_score 0-100).

    BUG #5 FIX — deduplicate required skills:
      Old: ["Python", "Python", "AWS"] → Python counted twice → inflated score.
      New: deduplicate required skills (preserve order) before matching.
    """
    # BUG #5 FIX: deduplicate while preserving order
    seen_req: set = set()
    unique_required: List[str] = []
    for s in skills_required:
        key = s.lower()
        if key not in seen_req:
            seen_req.add(key)
            unique_required.append(s)

    skill_map: Dict[str, int] = {s["name"].lower(): s["proficiency"] for s in emp_skills}
    matched: List[str] = []
    missing: List[str] = []
    prof_sum = 0.0

    for req in unique_required:
        req_l     = req.lower()
        best_prof = 0
        for sk_name, prof in skill_map.items():
            if req_l in sk_name or sk_name in req_l:
                best_prof = max(best_prof, prof)
        if best_prof:
            matched.append(req)
            prof_sum += best_prof
        else:
            missing.append(req)

    score = round(prof_sum / (5.0 * max(1, len(unique_required))) * 100, 1)
    return matched, missing, score


def matchmaker_node(state: GraphState) -> Dict[str, Any]:
    """
    Node 4 — Score, filter, rank candidates; collect disqualified.

    Fit Score = W_AVAILABILITY × availability_score
              + W_SKILL        × skill_match_score
              + W_RELIABILITY  × reliability_score
    """
    logger.info("[Node 4] matchmaker_node — scoring and ranking")

    reqs    = state["extracted_requirements"]  # type: ignore
    metrics = state["processed_metrics"]       # type: ignore
    erp     = state["raw_erp_data"]            # type: ignore

    skills_required: List[str] = reqs["skills_required"]
    hours_needed: float        = reqs["hours_per_week"]
    dept_reqs = reqs.get("department_requirements") or []
    department_only = bool(reqs.get("department_only"))
    allowed_departments = {
        (d.get("department") or "").strip().lower()
        for d in dept_reqs
        if d.get("department")
    }
    emp_lookup: Dict[str, Dict] = {e["id"]: e for e in erp["employees"]}

    ranked:       List[RankedCandidate]       = []
    disqualified: List[DisqualifiedCandidate] = []

    for m in metrics:
        eid = m["employee_id"]
        emp = emp_lookup[eid]
        if department_only and allowed_departments:
            if emp["department"].strip().lower() not in allowed_departments:
                continue
        bm  = m["bounty_metrics"]
        ld  = m["leave_detail"]

        matched, missing, skill_score = _compute_skill_match(
            emp["skills"], skills_required
        )

        if m["is_disqualified"]:
            # Determine HARD vs SOFT
            is_hard = (
                ld["is_fully_blocked"]
                or (m["overallocation_flag"] and m["available_hours_per_week"] <= 0)
                or m["capacity_hours_per_week"] <= 0
            )
            disqualified.append(DisqualifiedCandidate(
                employee_id             = eid,
                name                    = emp["name"],
                role                    = emp["role"],
                department              = emp["department"],
                disqualification_reason = m["disqualification_reason"],
                disqualification_type   = "HARD" if is_hard else "SOFT",
                leave_overlap_pct       = ld["overlap_pct"],
                leave_overlap_days      = ld["overlap_days"],
                overallocation_flag     = m["overallocation_flag"],
                bounty_summary          = {
                    "total":       bm["total_assigned"],
                    "completed":   bm["completed"],
                    "overdue":     bm["overdue"],
                    "in_progress": bm["in_progress"],
                    "reliability": bm["reliability_score"],
                },
            ))
            continue

        availability_score = m["availability_score"]
        reliability_score  = bm["reliability_score"]
        fit_score = round(
            W_AVAILABILITY * availability_score
            + W_SKILL       * skill_score
            + W_RELIABILITY * reliability_score,
            1,
        )

        # ── Human-readable signals ─────────────────────────────────────────
        match_reasons: List[str] = []
        warnings:      List[str] = []

        if matched:
            match_reasons.append(
                f"Matches {len(matched)}/{len(set(s.lower() for s in skills_required))} "
                f"required skills: {', '.join(matched)}"
            )
        if missing:
            warnings.append(f"Missing skills: {', '.join(missing)}")

        if availability_score >= 80:
            match_reasons.append(f"High availability ({availability_score:.0f}/100)")
        elif availability_score >= 40:
            match_reasons.append(
                f"Moderate availability ({availability_score:.0f}/100) — "
                f"{m['available_hours_per_week']:.0f} h/wk free"
            )
        else:
            warnings.append(
                f"Low availability ({availability_score:.0f}/100) — "
                f"only {m['available_hours_per_week']:.0f} h/wk free"
            )

        if m["available_hours_per_week"] >= hours_needed:
            match_reasons.append(
                f"Covers full {hours_needed:.0f} h/wk requirement "
                f"({m['available_hours_per_week']:.0f} h/wk available)"
            )
        else:
            warnings.append(
                f"Only {m['available_hours_per_week']:.0f} h/wk available "
                f"vs. {hours_needed:.0f} h/wk needed — partial allocation only"
            )

        if m["overallocation_flag"]:
            warnings.append(
                f"⚠️  Over-allocated ({m['utilization_pct']:.0f}% utilisation) — "
                "manager approval required"
            )

        if bm["active_bounty_hours_weekly"] > 0:
            warnings.append(
                f"📋  {bm['in_progress']} active bounty task(s) consuming "
                f"~{bm['active_bounty_hours_weekly']:.1f} h/wk "
                f"({', '.join(bm['in_progress_titles'][:2])})"
            )

        if bm["overdue"] or bm["effectively_overdue"]:
            total_bad = bm["overdue"] + bm["effectively_overdue"]
            warnings.append(
                f"⏰  {total_bad} overdue bounty task(s) — "
                f"reliability score penalised ({reliability_score:.0f}/100)"
            )

        if reliability_score >= 90:
            match_reasons.append(
                f"Excellent delivery reliability ({reliability_score:.0f}/100 — "
                f"{bm['completed']}/{bm['total_assigned']} bounties completed)"
            )
        elif reliability_score >= 70:
            match_reasons.append(f"Good delivery history ({reliability_score:.0f}/100)")

        if ld["has_any_overlap"]:
            warnings.append(
                f"📅  Approved leave overlaps {ld['overlap_pct']:.0f}% of project "
                f"({ld['overlap_days']} days — merged unique days): "
                f"{'; '.join(ld['leave_periods'])}"
            )

        ranked.append(RankedCandidate(
            rank=0,
            employee_id=eid,
            name=emp["name"],
            role=emp["role"],
            department=emp["department"],
            hourly_rate_usd=emp["hourly_rate_usd"],
            fit_score=fit_score,
            availability_score=availability_score,
            skill_match_score=skill_score,
            reliability_score=reliability_score,
            matched_skills=matched,
            missing_skills=missing,
            available_hours_per_week=m["available_hours_per_week"],
            projected_free_date=m["projected_free_date"],
            overallocation_flag=m["overallocation_flag"],
            bounty_summary={
                "total":               bm["total_assigned"],
                "completed":           bm["completed"],
                "in_progress":         bm["in_progress"],
                "not_started":         bm["not_started"],
                "overdue":             bm["overdue"],
                "effectively_overdue": bm["effectively_overdue"],
                "active_drain_h_wk":   bm["active_bounty_hours_weekly"],
                "reliability_score":   bm["reliability_score"],
                "overdue_titles":      bm["overdue_titles"],
                "in_progress_titles":  bm["in_progress_titles"],
            },
            leave_overlap_pct=ld["overlap_pct"],
            leave_overlap_days=ld["overlap_days"],
            match_reasons=match_reasons,
            warnings=warnings,
        ))

    ranked.sort(
        key=lambda c: (c.fit_score, c.reliability_score, c.skill_match_score),
        reverse=True,
    )
    for i, c in enumerate(ranked):
        c.rank = i + 1

    department_recommendations: List[Dict[str, Any]] = []
    if dept_reqs:
        for dr in dept_reqs:
            dept_name = (dr.get("department") or "").strip()
            if not dept_name:
                continue
            people_required = max(1, int(dr.get("people_required") or 1))
            dept_skills = _dedupe_case_insensitive(dr.get("skills_required") or [])

            if department_only:
                pool = [c for c in ranked if c.department.lower() == dept_name.lower()]
                match_mode = "department_only"
            else:
                pool = [c for c in ranked if c.department.lower() == dept_name.lower()]
                if pool:
                    match_mode = "department"
                elif dept_skills:
                    pool = ranked
                    match_mode = "skills_fallback"
                else:
                    pool = []
                    match_mode = "none"

            candidates: List[Dict[str, Any]] = []
            for c in pool:
                dept_skill_score = c.skill_match_score
                if dept_skills:
                    _, _, dept_skill_score = _compute_skill_match(
                        emp_lookup[c.employee_id]["skills"], dept_skills
                    )
                dept_fit_score = round(
                    W_AVAILABILITY * c.availability_score
                    + W_SKILL       * dept_skill_score
                    + W_RELIABILITY * c.reliability_score,
                    1,
                )
                candidates.append({
                    "employee_id": c.employee_id,
                    "name": c.name,
                    "role": c.role,
                    "department": c.department,
                    "fit_score": c.fit_score,
                    "availability_score": c.availability_score,
                    "skill_match_score": c.skill_match_score,
                    "reliability_score": c.reliability_score,
                    "department_skill_match_score": dept_skill_score,
                    "department_fit_score": dept_fit_score,
                    "available_hours_per_week": c.available_hours_per_week,
                    "overallocation_flag": c.overallocation_flag,
                    "leave_overlap_pct": c.leave_overlap_pct,
                })

            candidates.sort(
                key=lambda r: (
                    r["department_fit_score"],
                    r["reliability_score"],
                    r["department_skill_match_score"],
                ),
                reverse=True,
            )
            department_recommendations.append({
                "department": dept_name,
                "people_required": people_required,
                "people_available": len(candidates),
                "people_shortage": max(0, people_required - len(candidates)),
                "skills_required": dept_skills,
                "match_mode": match_mode,
                "recommended": candidates[:people_required],
            })

    logger.info(
        "[Node 4] Ranked %d | Disqualified %d | Top: %s (fit=%.1f)",
        len(ranked), len(disqualified),
        ranked[0].name if ranked else "N/A",
        ranked[0].fit_score if ranked else 0,
    )
    return {
        "ranked_candidates":       [c.model_dump() for c in ranked],
        "disqualified_candidates": [d.model_dump() for d in disqualified],
        "department_recommendations": department_recommendations,
    }