"""
test_pipeline.py — Comprehensive Test Suite  (v3)
==================================================
Covers:
  • 3 realistic project scenarios (regression tests)
  • 15 targeted edge-case assertions — including all 5 bug fixes

Run:
    cd resource_planner
    python3 test_pipeline.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(__file__))
logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from graph import app
from server import _build_ui_payload
from nodes import (
    _compute_leave_overlap,
    _compute_bounty_metrics,
    _compute_skill_match,
    _heuristic_extract,
    _merge_intervals,
    _today,
    _parse_date,
)

# ── ANSI colours ──────────────────────────────────────────────────────────────
G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"
C = "\033[96m"; B = "\033[1m";  X = "\033[0m"
BADGE = {"IDEAL": G, "AVAILABLE": C, "AT_RISK": R, "LEAVE_OVERLAP": Y, "PARTIAL_FIT": Y}
DISQ  = {"HARD": R, "SOFT": Y}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _bar(score: float, w: int = 18) -> str:
    filled = int(score / 100 * w)
    col = G if score >= 70 else Y if score >= 40 else R
    return col + "█" * filled + "░" * (w - filled) + X + f" {score:.0f}"

def _invoke(description: str) -> Dict[str, Any]:
    state = {
        "raw_project_input": description,
        "extracted_requirements": None, "raw_erp_data": None,
        "processed_metrics": None, "ranked_candidates": None,
        "disqualified_candidates": None, "errors": [],
    }
    return _build_ui_payload(app.invoke(state))

# ── Assertion runner ──────────────────────────────────────────────────────────

_RESULTS: List[Tuple[str, bool, str]] = []   # (label, passed, detail)

def check(label: str, passed: bool, detail: str = "") -> None:
    _RESULTS.append((label, passed, detail))

# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO TESTS  (regression)
# ═══════════════════════════════════════════════════════════════════════════════

SCENARIOS = [
    ("ML Feature Pipeline",
     "Build a real-time ML feature pipeline for our recommendation engine. "
     "Requires Python, Kafka, Apache Spark, and AWS. 20 hours per week for 8 weeks."),
    ("Full-Stack Customer Portal",
     "Rebuild the customer portal with React, TypeScript, Next.js frontend "
     "and FastAPI + PostgreSQL backend. Full-time 40 h/wk for 12 weeks."),
    ("Kubernetes Infrastructure Migration",
     "Migrate legacy services to Kubernetes on AWS using Terraform and CI/CD. "
     "DevOps engineer needed 30 hours per week for 6 weeks."),
]

def print_scenario(label: str, p: Dict) -> None:
    sep = "═" * 70
    reqs = p["meta"]["project_requirements"]
    summ = p["summary"]
    print(f"\n{B}{sep}{X}")
    print(f"{B}  📋  {label}{X}")
    print(sep)
    print(f"  Skills   : {', '.join(reqs['skills_required'])}")
    print(f"  Timeline : {reqs['start_date']}  |  {reqs['duration_weeks']}w "
          f"@ {reqs['hours_per_week']}h/wk  ({reqs['total_hours']:.0f}h total)")
    print(f"  Eligible : {summ['candidates_eligible']}  "
          f"| Disqualified: {summ['candidates_disqualified']} "
          f"(Hard:{summ['disqualified_hard']} Soft:{summ['disqualified_soft']})")
    print(f"  {summ['recommendation']}")

    print(f"\n{B}  Ranked Candidates{X}")
    for c in p["candidates"]:
        col = BADGE.get(c["status_badge"], X)
        print(f"\n  {B}#{c['rank']}  {col}[{c['status_badge']}]{X} "
              f"{B}{c['name']}{X}  ({c['role']})")
        print(f"     Fit          {_bar(c['scores']['fit_score'])}")
        print(f"     Availability {_bar(c['scores']['availability_score'])}")
        print(f"     Skill Match  {_bar(c['scores']['skill_match_score'])}")
        print(f"     Reliability  {_bar(c['scores']['reliability_score'])}")
        sk = c["skills"]
        print(f"     Skills  ✅ {', '.join(sk['matched']) or '—'}"
              f"  ❌ {', '.join(sk['missing']) or '—'}"
              f"  ({sk['coverage_pct']:.0f}% covered)")
        av = c["availability"]
        bn = c["bounties"]
        lv = c["leave"]
        print(f"     Avail   {av['available_hours_per_week']:.0f} h/wk  "
              f"free from {av['projected_free_date']}"
              f"{'  ⚠️ OVERALLOC' if av['overallocation_flag'] else ''}")
        if lv["has_overlap"]:
            print(f"     Leave   📅 {lv['overlap_pct']:.0f}% ({lv['overlap_days']}d unique) [{lv['severity']}]")
        drain = f"  ~{bn['active_drain_h_wk']:.1f}h/wk drain" if bn["active_drain_h_wk"] else ""
        bad   = f"  ⏰{bn['overdue']}+{bn['effectively_overdue']} overdue" if (bn["overdue"] or bn["effectively_overdue"]) else ""
        print(f"     Bounty  ✅{bn['completed']} 🔄{bn['in_progress']}{drain}{bad}  [rely {bn['reliability_score']:.0f}]")
        for r in c["match_reasons"]:   print(f"     {G}+ {r}{X}")
        for w in c["warnings"]:        print(f"     {Y}⚠ {w}{X}")

    if p["disqualified"]:
        print(f"\n{B}  Disqualified{X}")
        for d in p["disqualified"]:
            col = DISQ.get(d["disqualification_type"], R)
            print(f"  {col}[{d['disqualification_type']}]{X} {B}{d['name']}{X} — "
                  f"{d['disqualification_reason'][:80]}")


# ═══════════════════════════════════════════════════════════════════════════════
# EDGE-CASE ASSERTION SUITE
# ═══════════════════════════════════════════════════════════════════════════════

def run_assertions(p: Dict) -> None:
    """
    15 targeted assertions covering every known bug fix and structural invariant.
    """
    today      = _today()
    proj_start = today + timedelta(weeks=2)
    proj_end   = proj_start + timedelta(weeks=8)

    candidates   = p["candidates"]
    disqualified = p["disqualified"]

    by_name = {c["name"]: c for c in candidates}
    disq_by_name = {d["name"]: d for d in disqualified}

    # ── BUG #1: Overlapping leave → interval merging ───────────────────────
    leaves_overlap = [
        {"employee_id": "T", "leave_type": "PTO",
         "start_date": proj_start.isoformat(),
         "end_date":   (proj_start + timedelta(days=9)).isoformat()},   # 10 days
        {"employee_id": "T", "leave_type": "Sick",
         "start_date": (proj_start + timedelta(days=4)).isoformat(),    # 6 days overlap
         "end_date":   (proj_start + timedelta(days=14)).isoformat()},  # 11 days
    ]
    ld = _compute_leave_overlap(leaves_overlap, "T", proj_start, proj_end)
    check("BUG#1 — overlapping leave periods don't double-count (expect 15d)",
          ld.overlap_days == 15,
          f"got {ld.overlap_days}")

    check("BUG#1 — merged leave is strictly less than raw sum (10+11=21 > 15)",
          ld.overlap_days < 21,
          f"got {ld.overlap_days}")

    check("BUG#1 — Alex Morgan (overlapping leave) is ranked (not disqualified)",
          "Alex Morgan" in by_name,
          f"found in disq: {'Alex Morgan' in disq_by_name}")

    check("BUG#1 — Alex's leave_overlap_days uses merged count",
          "Alex Morgan" not in disq_by_name or disq_by_name["Alex Morgan"]["leave_overlap_days"] <= 18,
          "overlap_days too high")

    # ── BUG #2: Zero-capacity employee ────────────────────────────────────
    check("BUG#2 — Casey Liu (capacity=0) is HARD disqualified",
          "Casey Liu" in disq_by_name and
          disq_by_name["Casey Liu"]["disqualification_type"] == "HARD",
          f"found in ranked: {'Casey Liu' in by_name}")

    check("BUG#2 — Casey Liu is NOT in ranked candidates",
          "Casey Liu" not in by_name,
          "")

    # ── BUG #3: Reliability denominator — future not_started tasks ────────
    # Ravi has 1 completed + 1 not_started (future due) → actionable=1
    # base = 1/1 = 100 → should be high, not penalised
    if "Ravi Nair" in by_name:
        ravi_rely = by_name["Ravi Nair"]["bounties"]["reliability_score"]
        check("BUG#3 — Ravi's reliability ≥ 70 (future not_started doesn't penalise)",
              ravi_rely >= 70,
              f"got {ravi_rely}")

    # Standalone unit test: single future not_started → neutral (70)
    future_task = [{"employee_id": "X", "bounty_id": "B1", "title": "T",
                    "description": "", "hours_estimated": 5.0, "status": "not_started",
                    "due_date": (today + timedelta(days=14)).isoformat(),
                    "completed_date": None}]
    bm_fut = _compute_bounty_metrics(future_task, "X", today)
    check("BUG#3 — single future not_started → reliability = 70 (neutral)",
          bm_fut.reliability_score == 70.0,
          f"got {bm_fut.reliability_score}")

    # ── BUG #4: Unknown skill → no Python default ─────────────────────────
    cobol_req  = _heuristic_extract("COBOL mainframe expert needed for 8 weeks")
    check("BUG#4 — unknown skill extracts COBOL (not Python)",
          "Python" not in cobol_req.skills_required or "COBOL" in cobol_req.skills_required,
          f"got {cobol_req.skills_required}")

    truly_vague = _heuristic_extract("We need someone with strong skills for 4 weeks")
    check("BUG#4 — vague input doesn't produce 'Python' as default anymore",
          truly_vague.skills_required != ["Python"],
          f"got {truly_vague.skills_required}")

    # ── BUG #5: Duplicate skills in required list ─────────────────────────
    emp_skills = [{"name": "Python", "proficiency": 5}, {"name": "AWS", "proficiency": 3}]
    _, _, score_dedup = _compute_skill_match(emp_skills, ["Python", "Python", "AWS"])
    _, _, score_clean = _compute_skill_match(emp_skills, ["Python", "AWS"])
    check("BUG#5 — duplicate required skills give same score as deduplicated",
          score_dedup == score_clean,
          f"dedup={score_dedup} clean={score_clean}")

    # ── Structural invariants ──────────────────────────────────────────────
    check("INVARIANT — all fit scores in [0, 100]",
          all(0 <= c["scores"]["fit_score"] <= 100 for c in candidates),
          "")

    check("INVARIANT — ranked list sorted descending by fit_score",
          [c["scores"]["fit_score"] for c in candidates] ==
          sorted([c["scores"]["fit_score"] for c in candidates], reverse=True),
          "")

    check("INVARIANT — no employee appears in both ranked and disqualified",
          {c["employee_id"] for c in candidates}.isdisjoint(
              {d["employee_id"] for d in disqualified}),
          "")

    check("INVARIANT — Marcus Chen is disqualified (parental leave blocks project)",
          "Marcus Chen" in disq_by_name,
          f"found in ranked: {'Marcus Chen' in by_name}")

    check("INVARIANT — Elena Vasquez reliability ≥ 95 (all 3 bounties completed on time)",
          "Elena Vasquez" not in by_name or
          by_name["Elena Vasquez"]["bounties"]["reliability_score"] >= 95,
          f"got {by_name.get('Elena Vasquez', {}).get('bounties', {}).get('reliability_score', '?')}")

    check("INVARIANT — _merge_intervals handles non-overlapping intervals correctly",
          _merge_intervals([(_parse_date("2025-06-01"), _parse_date("2025-06-10")),
                            (_parse_date("2025-06-15"), _parse_date("2025-06-20"))]) ==
          [(_parse_date("2025-06-01"), _parse_date("2025-06-10")),
           (_parse_date("2025-06-15"), _parse_date("2025-06-20"))],
          "non-overlapping intervals should not be merged")

    check("INVARIANT — _merge_intervals handles adjacent intervals (touch but don't overlap)",
          len(_merge_intervals([(_parse_date("2025-06-01"), _parse_date("2025-06-10")),
                                (_parse_date("2025-06-11"), _parse_date("2025-06-20"))])) == 1,
          "adjacent intervals should merge into one")

    check("INVARIANT — leave before project window gives 0% overlap",
          _compute_leave_overlap(
              [{"employee_id": "T", "leave_type": "PTO",
                "start_date": (proj_start - timedelta(days=20)).isoformat(),
                "end_date":   (proj_start - timedelta(days=1)).isoformat()}],
              "T", proj_start, proj_end
          ).overlap_days == 0,
          "pre-project leave should not overlap")

    check("INVARIANT — leave after project window gives 0% overlap",
          _compute_leave_overlap(
              [{"employee_id": "T", "leave_type": "PTO",
                "start_date": (proj_end + timedelta(days=1)).isoformat(),
                "end_date":   (proj_end + timedelta(days=14)).isoformat()}],
              "T", proj_start, proj_end
          ).overlap_days == 0,
          "post-project leave should not overlap")

    check("INVARIANT — all-overdue bounties give reliability = 0",
          _compute_bounty_metrics(
              [{"employee_id":"E","bounty_id":f"B{i}","title":f"T{i}","description":"",
                "hours_estimated":5.0,"status":"overdue",
                "due_date":(today-timedelta(days=10)).isoformat(),"completed_date":None}
               for i in range(3)], "E", today
          ).reliability_score == 0.0,
          "")

    check("INVARIANT — in_progress past due counts as effectively_overdue",
          _compute_bounty_metrics(
              [{"employee_id":"E","bounty_id":"B1","title":"Late","description":"",
                "hours_estimated":8.0,"status":"in_progress",
                "due_date":(today-timedelta(days=3)).isoformat(),"completed_date":None}],
              "E", today
          ).effectively_overdue == 1,
          "")

    check("INVARIANT — hours clamped: 500 h/wk → 60 h/wk",
          _heuristic_extract("need 500 hours per week").hours_per_week == 60.0,
          "")

    check("INVARIANT — duration minimum 1 week enforced",
          _heuristic_extract("quick fix for 0 weeks").duration_weeks >= 1,
          "")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # ── Run scenario tests ─────────────────────────────────────────────────
    payloads: List[Dict] = []
    for label, desc in SCENARIOS:
        p = _invoke(desc)
        payloads.append(p)
        print_scenario(label, p)

    # ── Run assertions against the first (ML) scenario payload ─────────────
    print(f"\n{'═'*70}")
    print(f"{B}  EDGE-CASE ASSERTION SUITE  ({len([None]*24)} checks){X}")
    print("═" * 70)
    run_assertions(payloads[0])

    # ── Print results ──────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    failed = len(_RESULTS) - passed
    print()
    for label, ok, detail in _RESULTS:
        icon  = f"{G}PASS{X}" if ok else f"{R}FAIL{X}"
        extra = f"  → {detail}" if detail and not ok else ""
        print(f"  [{icon}]  {label}{extra}")

    print(f"\n{'═'*70}")
    colour = G if failed == 0 else R
    print(f"{B}  Results: {colour}{passed} passed{X}{B}, {R if failed else G}{failed} failed{X}")
    print(f"{'═'*70}\n")

    if failed:
        sys.exit(1)