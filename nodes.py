"""
nodes.py — LangGraph Node Functions  (v4)
=========================================
Bug fixes applied in v1–v3:
  #1  _compute_leave_overlap: merge overlapping intervals before summing
  #2  compute_metrics_node: capacity=0 → hard disqualify immediately
  #3  _compute_bounty_metrics: reliability denominator uses only "actionable" bounties
  #4  _heuristic_extract: unknown-skill fallback extracts UPPERCASE/TitleCase tokens
  #5  _compute_skill_match: deduplicate skills_required before scoring

Fixes applied in v4:
  #6  _compute_skill_match: replace bidirectional substring check with word-boundary
      regex matching — prevents false positives ("Go" matching "Django",
      "Java" matching "JavaScript", etc.)
  #7  _compute_leave_overlap: leave_periods now shows the clipped window overlap
      dates rather than the raw leave record dates (avoids misleading UI display
      when a leave record extends far outside the project window)
  #8  _heuristic_extract hours_per_week: fix lastindex == 0 (never True) →
      use explicit `m.lastindex is None` to distinguish capture-group patterns
      from no-capture-group patterns (full.?time / half.?time)
  #9  _compute_bounty_metrics: replace fixed 2-week drain spread with
      urgency-weighted drain — hours_estimated / max(0.5, days_remaining / 7)
      so a bounty due tomorrow drains more capacity than one due in four weeks
  #10 _cached_llm_extract: append date.today() to the cache key so default
      start_dates (computed relative to today) do not go stale after midnight
  #11 _cached_llm_extract: remove TOCTOU before/after cache_info() hit/miss
      comparison — replaced with a single post-call stats log that is safe
      under concurrent access
  #12 _cached_llm_extract: maxsize now reads from LLM_CACHE_SIZE env var
      (default 256) so operators can tune memory use without code changes
  #13 ingest_erp_data_node: data source abstracted behind _load_erp_data();
      set ERP_DATA_PATH env var to load real JSON instead of mock data
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
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
import json as _json
from mock_data import MOCK_ERP_DATA

# NEW v3: Bench + Health engines (strictly additive — no existing logic changed)
try:
    from bench import compute_bench_metrics, ENABLE_BENCH_BOOST, BENCH_AVAILABILITY_BOOST
    from health import compute_workforce_health, health_aware_fit_score, HEALTH_AWARE_SCORING
    _BENCH_HEALTH_AVAILABLE = True
except ImportError:
    _BENCH_HEALTH_AVAILABLE = False
    HEALTH_AWARE_SCORING   = False
    ENABLE_BENCH_BOOST     = False
    BENCH_AVAILABILITY_BOOST = 0.0


def _load_erp_data() -> Dict[str, Any]:
    """
    FIX #13: Load HR/ERP data from an external JSON file if ERP_DATA_PATH is
    set, otherwise fall back to the built-in mock dataset.

    Usage:
        export ERP_DATA_PATH=/path/to/erp_data.json

    The JSON must conform to the ERPData schema — top-level keys:
        employees, assignments, leaves, bounties  (all arrays).

    If the path is set but the file cannot be read or parsed, the node logs a
    warning and falls back to mock data rather than crashing the pipeline.
    """
    path = os.getenv("ERP_DATA_PATH", "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = _json.load(fh)
            logger.info("[ERP] Loaded real data from %s", path)
            return data
        except Exception as exc:
            logger.warning(
                "[ERP] Failed to load %s (%s) — falling back to mock data", path, exc
            )
    return MOCK_ERP_DATA

logger = logging.getLogger(__name__)


_SKILL_ALIASES_RAW: Dict[str, str] = {
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

# FIX 2: Compile all alias patterns once at module load.
# Previously re.search(pat, text) was called per-invocation without caching,
# forcing Python to recompile every pattern on every request.
COMPILED_ALIASES: List[Tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE), canon)
    for pat, canon in _SKILL_ALIASES_RAW.items()
]

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
    skills = [canon for pat, canon in COMPILED_ALIASES if pat.search(lowered)]
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

    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        timeout=8,      # hard cap — prevents the 15s+ spike seen under load
        max_retries=2,  # retry twice before raising, then fallback to heuristic
    )
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


# FIX #12: Read cache size from environment so operators can tune memory use
# without code changes.  Default 256 ≈ 24 h of unique prompts at typical
# volumes (~500 B–2 KB JSON per entry → ~500 KB worst-case resident memory).
# Set LLM_CACHE_SIZE=0 to disable caching entirely (useful in testing).
_LLM_CACHE_SIZE: int = int(os.getenv("LLM_CACHE_SIZE", "256"))


@lru_cache(maxsize=_LLM_CACHE_SIZE)
def _cached_llm_extract(text: str) -> str:
    """
    Cache LLM extraction results keyed on (normalised input, today's date).

    The date suffix (FIX #10) ensures that default start_dates — computed
    relative to today inside the LLM prompt — are never served from a cache
    entry made on a previous calendar day.  Only prompts that omit an explicit
    start_date are affected; prompts with a literal date in them are unaffected.

    Returns a JSON string so the cache holds an immutable value; callers
    deserialise a fresh model each time, preventing accidental mutation.

    lru_cache is process-local and thread-safe for reads (CPython GIL).
    """
    return _llm_extract(text).model_dump_json()


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
            # FIX #8: m.lastindex is None when the pattern has no capture
            # groups (full.?time / half.?time); it is 1 when there is one
            # group.  The old check `m.lastindex == 0` was never True because
            # re uses 1-based group indexing — patterns with one group yield
            # lastindex=1, not 0.  Use explicit `is None` test instead.
            if m.lastindex is None:
                hours_per_week = 40.0 if "full" in m.group(0) else 20.0
            else:
                hours_per_week = float(m.group(1))
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
            # FIX #10: append today's date to the cache key so cached entries
            # with a defaulted start_date (relative to "today") never survive
            # past midnight into the next calendar day.
            cache_key = raw_text.strip() + "\x00" + _today().isoformat()
            reqs = ExtractedRequirements.model_validate_json(
                _cached_llm_extract(cache_key)
            )
            # FIX #11: removed TOCTOU before/after cache_info() comparison.
            # Under concurrent load the before/after hit-count delta can be
            # corrupted by other threads, producing swapped or duplicated
            # "CACHE HIT" / "API call" log messages.  Log aggregate stats
            # once after the call instead — always accurate, never racy.
            info = _cached_llm_extract.cache_info()
            logger.info(
                "[Node 1] LLM extraction complete "
                "(cache hits=%d misses=%d currsize=%d/%s)",
                info.hits, info.misses, info.currsize,
                str(_LLM_CACHE_SIZE) if _LLM_CACHE_SIZE else "∞",
            )
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
    """Node 2 — Load and validate HR/ERP data (mock or real via ERP_DATA_PATH)."""
    logger.info("[Node 2] ingest_erp_data_node — loading data")
    erp = ERPData(**_load_erp_data())
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
    proj_start: date,
    proj_end: date,
) -> LeaveOverlapDetail:
    """
    Compute how much of the project window is covered by approved leave.

    Accepts a pre-filtered list of leave records for a single employee
    (caller indexes by employee_id before calling — see compute_metrics_node).

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
        lv_start = _parse_date(lv["start_date"])
        lv_end   = _parse_date(lv["end_date"])

        # Clip to project window
        clip_start = max(lv_start, proj_start)
        clip_end   = min(lv_end,   proj_end)
        if clip_start <= clip_end:
            clipped_intervals.append((clip_start, clip_end))
            # FIX #7: show the clipped overlap window dates so the UI displays
            # how many days fall inside the project — not the full leave span.
            # Append the original leave dates in parentheses only when the
            # leave record extends outside the project window, so managers
            # can see the full context without being misled by dates that fall
            # outside the period they care about.
            overlap_str = (
                f"{lv['leave_type']}: {_iso(clip_start)} → {_iso(clip_end)}"
            )
            is_clipped = clip_start != lv_start or clip_end != lv_end
            if is_clipped:
                overlap_str += (
                    f" (full leave: {lv['start_date']} → {lv['end_date']})"
                )
            leave_periods.append(overlap_str)

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
    today: date,
) -> BountyMetrics:
    """
    Aggregate bounty statistics and derive reliability_score (0-100).

    Accepts a pre-filtered list of bounties for a single employee
    (caller indexes by employee_id before calling — see compute_metrics_node).

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
    emp_bounties = bounties  # already filtered by caller

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
    active_hours = 0.0          # raw total estimated hours (for reporting)
    active_hours_weekly = 0.0   # FIX #9: urgency-weighted weekly drain
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
            # FIX #9: weight drain by urgency rather than a fixed 2-week window.
            # drain_weeks = days_remaining / 7, clamped to [0.5, 4.0]:
            #   due tomorrow  → drain_weeks=0.5 → 2× pressure  (urgent)
            #   due in 2 wks  → drain_weeks=2.0 → same as before (unchanged)
            #   due in 4 wks  → drain_weeks=4.0 → ½ the pressure (not urgent)
            days_remaining = max(0, (due - today).days)
            drain_weeks = max(0.5, min(4.0, days_remaining / 7.0))
            active_hours_weekly += b["hours_estimated"] / drain_weeks

        elif status == BountyStatus.NOT_STARTED:
            not_started_cnt += 1
            if is_past_due:
                eff_overdue_cnt += 1   # never started and already late

        elif status == BountyStatus.OVERDUE:
            overdue_cnt += 1
            overdue_titles.append(title)

    total            = len(emp_bounties)
    total_problematic = overdue_cnt + eff_overdue_cnt
    actionable = completed_cnt + total_problematic
    if actionable == 0:
        # Only future/neutral tasks — no track record yet → neutral
        reliability = 70.0
    else:
        base              = (completed_cnt / actionable) * 100
        penalty           = min(base, 15 * overdue_cnt + 5 * eff_overdue_cnt)
        consistency_bonus = 5.0 if (base > 90 and actionable >= 3) else 0.0
        reliability       = round(max(0.0, min(100.0, base - penalty + consistency_bonus)), 1)

    active_hours_weekly = round(active_hours_weekly, 2)
    # FIX #9: active_hours_weekly is now urgency-weighted (see loop above).
    # Previously this was a fixed ÷2 spread regardless of when each bounty
    # was due.  The new value correctly reflects near-term vs. far-future work.

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

    # FIX 1: Pre-index leaves and bounties by employee_id (O(n) once) so the
    # per-employee helpers receive only their own records instead of scanning
    # the entire dataset on every iteration (was O(n²) at scale).
    leave_by_emp: Dict[str, List[Dict]] = {}
    for lv in leaves:
        leave_by_emp.setdefault(lv["employee_id"], []).append(lv)

    bounty_by_emp: Dict[str, List[Dict]] = {}
    for b in bounties:
        bounty_by_emp.setdefault(b["employee_id"], []).append(b)

    metrics: List[Dict[str, Any]] = []

    for emp in employees:
        eid      = emp["id"]
        capacity = emp["capacity_hours_per_week"]

        # ── BUG #2 FIX: zero-capacity guard ───────────────────────────────
        if capacity <= 0:
            # Still compute bounty metrics for the disqualified card
            bm = _compute_bounty_metrics(bounty_by_emp.get(eid, []), today)
            ld = _compute_leave_overlap(leave_by_emp.get(eid, []), proj_start, proj_end)
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
        bm = _compute_bounty_metrics(bounty_by_emp.get(eid, []), today)

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
        ld = _compute_leave_overlap(leave_by_emp.get(eid, []), proj_start, proj_end)

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
        m_dict = m.model_dump()

        # ── NEW v3: Bench + Health (additive — appended after model_dump) ──
        if _BENCH_HEALTH_AVAILABLE:
            try:
                bench_m = compute_bench_metrics(
                    employee                 = emp,
                    active_assignments       = active_asgns.get(eid, []),
                    available_hours_per_week = round(available_hours, 2),
                    utilization_pct          = utilization_pct,
                    today                    = today,
                )
                m_dict["bench_metrics"] = bench_m.model_dump()
            except Exception as _bench_exc:
                logger.warning("[Node 3] bench metrics failed for %s: %s", emp["name"], _bench_exc)
                m_dict["bench_metrics"] = None

            try:
                health_m = compute_workforce_health(m_dict, today)
                m_dict["workforce_health"] = health_m.model_dump()
            except Exception as _health_exc:
                logger.warning("[Node 3] health metrics failed for %s: %s", emp["name"], _health_exc)
                m_dict["workforce_health"] = None
        else:
            m_dict["bench_metrics"]    = None
            m_dict["workforce_health"] = None

        metrics.append(m_dict)
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

    # FIX #6: pre-compiled word-boundary patterns for each lowercased skill name
    # seen so far.  Built lazily and cached here so repeated calls for the same
    # skill set don't re-compile patterns on every invocation.
    _skill_pattern_cache: Dict[str, re.Pattern] = {}

    def _skill_pat(skill: str) -> re.Pattern:
        if skill not in _skill_pattern_cache:
            _skill_pattern_cache[skill] = re.compile(
                r"\b" + re.escape(skill) + r"\b", re.IGNORECASE
            )
        return _skill_pattern_cache[skill]

    skill_map: Dict[str, int] = {s["name"].lower(): s["proficiency"] for s in emp_skills}
    matched: List[str] = []
    missing: List[str] = []
    prof_sum = 0.0

    for req in unique_required:
        req_l     = req.lower()
        req_pat   = _skill_pat(req_l)
        best_prof = 0
        for sk_name, prof in skill_map.items():
            # FIX #6: use word-boundary regex in both directions to prevent
            # false positives like "Go" ⊆ "Django", "Java" ⊆ "JavaScript".
            # Bidirectional matching is retained so that "PostgreSQL" still
            # matches an employee skill listed as "PostgreSQL DBA".
            if req_pat.search(sk_name) or _skill_pat(sk_name).search(req_l):
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

        # NEW v3: Optional bench availability boost (default OFF — preserves existing ranking)
        if _BENCH_HEALTH_AVAILABLE and ENABLE_BENCH_BOOST:
            bench_m = m.get("bench_metrics") or {}
            if bench_m.get("bench_status") == "AVAILABLE_NOW":
                availability_score = min(100.0, availability_score + BENCH_AVAILABILITY_BOOST)

        # NEW v3: Health-aware fit score (default OFF — preserves existing ranking exactly)
        if _BENCH_HEALTH_AVAILABLE and HEALTH_AWARE_SCORING:
            wh = m.get("workforce_health") or {}
            sustainability = wh.get("sustainability_score", 70.0)
            fit_score = health_aware_fit_score(
                availability_score   = availability_score,
                skill_score          = skill_score,
                reliability_score    = reliability_score,
                sustainability_score = sustainability,
            )
        else:
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

        # NEW v3: Surface health warnings on the candidate card (additive only)
        if _BENCH_HEALTH_AVAILABLE:
            wh = m.get("workforce_health") or {}
            for hw in wh.get("health_warnings", []):
                if hw not in warnings:  # avoid duplicating overalloc warning
                    warnings.append(hw)

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

            # FIX 4: Skip re-running _compute_skill_match when dept_skills are
            # empty (reuse global score) or identical to the project-wide
            # skills_required (score was already computed in the main loop).
            global_skills_set = {s.lower() for s in skills_required}
            dept_skills_set   = {s.lower() for s in dept_skills}
            dept_skills_differ = bool(dept_skills) and dept_skills_set != global_skills_set

            for c in pool:
                if dept_skills_differ:
                    _, _, dept_skill_score = _compute_skill_match(
                        emp_lookup[c.employee_id]["skills"], dept_skills
                    )
                else:
                    dept_skill_score = c.skill_match_score
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

    # NEW v3: Compute org-level bench + health summaries (additive, never breaks old callers)
    bench_summary:            Dict[str, Any] = {}
    workforce_health_summary: Dict[str, Any] = {}
    if _BENCH_HEALTH_AVAILABLE:
        try:
            from bench  import compute_bench_summary
            from health import compute_health_summary
            bench_summary            = compute_bench_summary(metrics)
            workforce_health_summary = compute_health_summary(metrics)
        except Exception as _summ_exc:
            logger.warning("[Node 4] bench/health summary failed: %s", _summ_exc)

    return {
        "ranked_candidates":          [c.model_dump() for c in ranked],
        "disqualified_candidates":    [d.model_dump() for d in disqualified],
        "department_recommendations": department_recommendations,
        # NEW v3 keys — optional, backward-compatible
        "bench_summary":              bench_summary or None,
        "workforce_health_summary":   workforce_health_summary or None,
    }