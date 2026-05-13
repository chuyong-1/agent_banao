"""
server.py — MCP Server: Resource-Planner-Server  (v3)
======================================================
Run locally:
    python server.py

The UI payload now contains the following top-level sections:
    meta          — extracted project requirements
    summary       — aggregate stats + recommendation
    candidates    — ranked eligible employees (with bounty + leave detail)
    disqualified  — employees excluded (HARD or SOFT), with reasons
    departments   — department-wise recommendations (if provided)
    pipeline_warnings — non-fatal processing errors

v3 changes:
    • Input length guard: inputs over MAX_INPUT_CHARS (default 4 000, env-
      configurable) are truncated at a sentence boundary before entering the
      pipeline.  A warning is surfaced in pipeline_warnings.
"""

from __future__ import annotations

import json
import logging
import sys
import os
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(__file__))

from mcp.server.fastmcp import FastMCP
from graph import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("resource-planner-server")

mcp = FastMCP(
    name="Resource-Planner-Server",
    instructions=(
        "AI-powered resource availability planner. "
        "Accepts a natural-language project description and returns a ranked "
        "list of best-fit employees with availability, skill match, bounty "
        "reliability scores, leave overlap details, and disqualification reasons."
    ),
)


# ── UI payload builder ────────────────────────────────────────────────────────

def _build_ui_payload(final_state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transform raw LangGraph final state into a structured UI-ready payload.

    Shape:
    {
      "meta":               { project requirements },
      "summary":            { aggregate stats + recommendation },
      "candidates":         [ ranked cards with all scoring detail ],
      "disqualified":       [ excluded employees with reasons ],
            "pipeline_warnings":  [ non-fatal errors ],
            "departments":        [ department-wise recommendations ]
    }
    """
    reqs         = final_state.get("extracted_requirements") or {}
    candidates   = final_state.get("ranked_candidates")      or []
    disqualified = final_state.get("disqualified_candidates") or []
    dept_recs    = final_state.get("department_recommendations") or []
    errors       = final_state.get("errors")                  or []

    today_str = date.today().isoformat()

    # ── Meta ──────────────────────────────────────────────────────────────
    meta: Dict[str, Any] = {
        "generated_at": today_str,
        "project_requirements": {
            "skills_required": reqs.get("skills_required", []),
            "department_requirements": reqs.get("department_requirements", []),
            "department_only": reqs.get("department_only", False),
            "start_date":      reqs.get("start_date", ""),
            "duration_weeks":  reqs.get("duration_weeks", 0),
            "hours_per_week":  reqs.get("hours_per_week", 0),
            "total_hours":     reqs.get("duration_weeks", 0) * reqs.get("hours_per_week", 0),
        },
    }

    # ── Summary ───────────────────────────────────────────────────────────
    top_fit        = candidates[0]["fit_score"] if candidates else 0.0

    # FIX 5: Compute all summary stats in a single pass over candidates
    # instead of five separate sum()/max() calls that each traverse the list.
    top_reliability = overalloc_count = with_leave_count = with_bounty_issues = 0
    for c in candidates:
        top_reliability   = max(top_reliability, c["reliability_score"])
        overalloc_count  += c["overallocation_flag"]
        with_leave_count += c["leave_overlap_pct"] > 0
        with_bounty_issues += bool(
            c["bounty_summary"].get("overdue") or
            c["bounty_summary"].get("effectively_overdue")
        )
    hard_disq = sum(1 for d in disqualified if d["disqualification_type"] == "HARD")
    soft_disq = sum(1 for d in disqualified if d["disqualification_type"] == "SOFT")

    if top_fit >= 70:
        recommendation = "✅ Strong candidates available — proceed with selection."
    elif top_fit >= 40:
        recommendation = "⚠️  Limited availability — consider timeline adjustment."
    else:
        recommendation = "🚫  No well-matched candidates — escalate to resource manager."

    summary: Dict[str, Any] = {
        "total_evaluated":            len(candidates) + len(disqualified),
        "candidates_eligible":        len(candidates),
        "candidates_disqualified":    len(disqualified),
        "disqualified_hard":          hard_disq,
        "disqualified_soft":          soft_disq,
        "departments_requested":      len(reqs.get("department_requirements", [])),
        "departments_with_shortage":  sum(1 for d in dept_recs if d.get("people_shortage", 0) > 0),
        "candidates_overallocated":   overalloc_count,
        "candidates_with_leave":      with_leave_count,
        "candidates_with_bounty_issues": with_bounty_issues,
        "top_fit_score":              top_fit,
        "top_reliability_score":      top_reliability,
        "recommendation":             recommendation,
        "scoring_weights": {
            "availability":  "45%",
            "skill_match":   "30%",
            "reliability":   "25%",
        },
    }

    # ── Candidate cards ───────────────────────────────────────────────────
    def _status_badge(c: Dict) -> str:
        if c["fit_score"] >= 75 and not c["overallocation_flag"] and c["leave_overlap_pct"] == 0:
            return "IDEAL"
        if c["fit_score"] >= 60:
            return "AVAILABLE"
        if c["overallocation_flag"]:
            return "AT_RISK"
        if c["leave_overlap_pct"] > 0:
            return "LEAVE_OVERLAP"
        return "PARTIAL_FIT"

    BADGE_ICONS = {
        "IDEAL": "🟢", "AVAILABLE": "🔵",
        "AT_RISK": "🔴", "LEAVE_OVERLAP": "🟠", "PARTIAL_FIT": "🟡",
    }

    candidate_cards: List[Dict[str, Any]] = []
    for c in candidates:
        badge = _status_badge(c)
        bm    = c["bounty_summary"]
        card: Dict[str, Any] = {
            # Identity
            "rank":            c["rank"],
            "employee_id":     c["employee_id"],
            "name":            c["name"],
            "role":            c["role"],
            "department":      c["department"],
            "hourly_rate_usd": c["hourly_rate_usd"],
            "status_badge":    badge,
            "status_icon":     BADGE_ICONS.get(badge, "⚪"),

            # Three-dimension scores
            "scores": {
                "fit_score":           c["fit_score"],
                "availability_score":  c["availability_score"],
                "skill_match_score":   c["skill_match_score"],
                "reliability_score":   c["reliability_score"],
            },

            # Skill breakdown
            "skills": {
                "matched":      c["matched_skills"],
                "missing":      c["missing_skills"],
                "coverage_pct": round(
                    len(c["matched_skills"]) /
                    max(1, len(c["matched_skills"]) + len(c["missing_skills"])) * 100, 1
                ),
            },

            # Availability
            "availability": {
                "available_hours_per_week": c["available_hours_per_week"],
                "projected_free_date":      c["projected_free_date"],
                "overallocation_flag":      c["overallocation_flag"],
            },

            # Leave (granular)
            "leave": {
                "has_overlap":       c["leave_overlap_pct"] > 0,
                "overlap_pct":       c["leave_overlap_pct"],
                "overlap_days":      c["leave_overlap_days"],
                "severity": (
                    "NONE"    if c["leave_overlap_pct"] == 0 else
                    "PARTIAL" if c["leave_overlap_pct"] < 25 else
                    "NOTABLE"
                ),
            },

            # Bounty reliability
            "bounties": {
                "total_assigned":     bm.get("total", 0),
                "completed":          bm.get("completed", 0),
                "in_progress":        bm.get("in_progress", 0),
                "not_started":        bm.get("not_started", 0),
                "overdue":            bm.get("overdue", 0),
                "effectively_overdue": bm.get("effectively_overdue", 0),
                "active_drain_h_wk":  bm.get("active_drain_h_wk", 0.0),
                "reliability_score":  bm.get("reliability_score", 70.0),
                "overdue_titles":     bm.get("overdue_titles", []),
                "in_progress_titles": bm.get("in_progress_titles", []),
            },

            # Human-readable signals
            "match_reasons": c["match_reasons"],
            "warnings":      c["warnings"],
        }
        candidate_cards.append(card)

    # ── Disqualified section ──────────────────────────────────────────────
    disq_cards: List[Dict[str, Any]] = []
    for d in disqualified:
        disq_cards.append({
            "employee_id":             d["employee_id"],
            "name":                    d["name"],
            "role":                    d["role"],
            "department":              d["department"],
            "disqualification_type":   d["disqualification_type"],
            "disqualification_reason": d["disqualification_reason"],
            "leave_overlap_pct":       d["leave_overlap_pct"],
            "leave_overlap_days":      d["leave_overlap_days"],
            "overallocation_flag":     d["overallocation_flag"],
            "bounties": {
                "total":       d["bounty_summary"].get("total", 0),
                "completed":   d["bounty_summary"].get("completed", 0),
                "overdue":     d["bounty_summary"].get("overdue", 0),
                "reliability": d["bounty_summary"].get("reliability", 70.0),
            },
        })

    return {
        "meta":              meta,
        "summary":           summary,
        "candidates":        candidate_cards,
        "disqualified":      disq_cards,
        "departments":       dept_recs,
        "pipeline_warnings": errors,
    }


# Maximum characters forwarded to the LLM / heuristic parser.
# gpt-4o-mini's context window is large, but very long pastes (full SOWs,
# email threads) add latency and cost without improving extraction quality.
# Inputs over this limit are truncated at a sentence boundary if possible,
# or hard-truncated otherwise.  A warning is added to pipeline_warnings.
_MAX_INPUT_CHARS: int = int(os.getenv("MAX_INPUT_CHARS", "4000"))


def _truncate_input(text: str, limit: int) -> Tuple[str, Optional[str]]:
    """
    Truncate *text* to *limit* characters, preferring a sentence boundary.
    Returns (truncated_text, warning_message | None).
    """
    if len(text) <= limit:
        return text, None
    # Try to cut at the last sentence-ending punctuation before the limit.
    boundary = max(
        text.rfind(". ", 0, limit),
        text.rfind(".\n", 0, limit),
        text.rfind("! ",  0, limit),
        text.rfind("? ",  0, limit),
    )
    cut = (boundary + 1) if boundary > limit // 2 else limit
    truncated = text[:cut].rstrip()
    warning = (
        f"Input truncated from {len(text):,} to {len(truncated):,} characters "
        f"(limit: {limit:,}).  Extraction may be incomplete."
    )
    return truncated, warning

@mcp.tool()
def analyze_resource_allocation(project_description: str) -> str:
    """
    Analyse a natural-language project description and return a full
    resource availability report as a JSON string.

    The report covers:
      • Extracted project requirements (skills, dates, hours)
      • Ranked eligible candidates scored on availability (45%),
        skill match (30%), and bounty reliability (25%)
      • Per-candidate leave overlap analysis with severity
      • Per-candidate bounty breakdown (completed / in_progress /
        overdue / effectively_overdue) and active hour drain
      • Disqualified employees (hard: fully on leave or zero capacity;
        soft: >50% leave overlap) with explicit reasons
            • Department-wise requirements (skills + headcount) and
                recommended candidates per department (if provided)
      • Aggregate summary stats and an AI recommendation

    Args:
        project_description: Free-form text — Jira ticket, SOW excerpt,
            Slack message, or any description mentioning required skills,
            timeline, and weekly commitment.

    Returns:
        JSON string with keys: meta, summary, candidates, disqualified,
        departments, pipeline_warnings.
    """
    logger.info("Tool invoked — input length: %d chars", len(project_description))

    if not project_description or not project_description.strip():
        return json.dumps({"error": "project_description must not be empty.", "candidates": []})

    # FIX #6: guard against oversized inputs that would exceed the LLM context
    # window or add unnecessary latency.  Truncation is logged and surfaced in
    # pipeline_warnings so callers know extraction may be incomplete.
    truncation_warning: Optional[str] = None
    project_description, truncation_warning = _truncate_input(
        project_description.strip(), _MAX_INPUT_CHARS
    )
    if truncation_warning:
        logger.warning("Input truncated: %s", truncation_warning)

    try:
        initial_state = {
            "raw_project_input":       project_description,
            "extracted_requirements":  None,
            "raw_erp_data":            None,
            "processed_metrics":       None,
            "ranked_candidates":       None,
            "disqualified_candidates": None,
            "errors":                  ([truncation_warning] if truncation_warning else []),
        }
        final_state = app.invoke(initial_state)
        payload     = _build_ui_payload(final_state)
        logger.info(
            "Pipeline complete — ranked: %d  disqualified: %d",
            len(payload["candidates"]),
            len(payload["disqualified"]),
        )
        return json.dumps(payload, indent=2, default=str)

    except Exception as exc:
        logger.exception("Pipeline failed")
        return json.dumps({"error": str(exc), "type": type(exc).__name__, "candidates": []}, indent=2)


if __name__ == "__main__":
    logger.info("Starting Resource-Planner-Server (stdio transport)...")
    mcp.run(transport="stdio")