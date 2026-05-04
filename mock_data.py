"""
mock_data.py — Realistic Mock HR / ERP Dataset  (v3 — Edge Case Employees Added)
=================================================================================
Two new employees added to exercise previously untested edge cases:

  EMP-009 "Alex Morgan"   — has two OVERLAPPING leave periods in the project window
                            (exercises Bug #1 fix: interval merging)
  EMP-010 "Casey Liu"     — capacity_hours_per_week = 0 (inactive / on LOA)
                            (exercises Bug #2 fix: zero-capacity hard disqualify)

Existing employees unchanged so all prior test assertions still hold.
"""

from __future__ import annotations

from datetime import date, timedelta

_TODAY = date.today()
_FMT   = lambda d: d.isoformat()


MOCK_ERP_DATA: dict = {

    # ──────────────────────────────────────────────────────────────────────
    # EMPLOYEES
    # ──────────────────────────────────────────────────────────────────────
    "employees": [
        {
            "id": "EMP-001", "name": "Aisha Patel",
            "role": "Senior Backend Engineer", "department": "Platform Engineering",
            "capacity_hours_per_week": 40.0, "hourly_rate_usd": 95.0,
            "skills": [
                {"name": "Python",     "proficiency": 5},
                {"name": "FastAPI",    "proficiency": 5},
                {"name": "PostgreSQL", "proficiency": 4},
                {"name": "AWS",        "proficiency": 4},
                {"name": "Docker",     "proficiency": 4},
                {"name": "Kafka",      "proficiency": 3},
                {"name": "Redis",      "proficiency": 3},
            ],
        },
        {
            "id": "EMP-002", "name": "Marcus Chen",
            "role": "ML Engineer", "department": "AI/ML",
            "capacity_hours_per_week": 40.0, "hourly_rate_usd": 110.0,
            "skills": [
                {"name": "Python",           "proficiency": 5},
                {"name": "Machine Learning", "proficiency": 5},
                {"name": "PyTorch",          "proficiency": 4},
                {"name": "LangChain",        "proficiency": 4},
                {"name": "AWS",              "proficiency": 3},
                {"name": "Docker",           "proficiency": 3},
                {"name": "Kubernetes",       "proficiency": 2},
            ],
        },
        {
            "id": "EMP-003", "name": "Sofia Rossi",
            "role": "Frontend Engineer", "department": "Product Engineering",
            "capacity_hours_per_week": 40.0, "hourly_rate_usd": 85.0,
            "skills": [
                {"name": "React",        "proficiency": 5},
                {"name": "TypeScript",   "proficiency": 5},
                {"name": "Next.js",      "proficiency": 4},
                {"name": "GraphQL",      "proficiency": 3},
                {"name": "CSS/Tailwind", "proficiency": 5},
                {"name": "Python",       "proficiency": 2},
            ],
        },
        {
            "id": "EMP-004", "name": "Jamal Williams",
            "role": "DevOps / Platform Engineer", "department": "Infrastructure",
            "capacity_hours_per_week": 40.0, "hourly_rate_usd": 100.0,
            "skills": [
                {"name": "Kubernetes",  "proficiency": 5},
                {"name": "Terraform",   "proficiency": 5},
                {"name": "AWS",         "proficiency": 5},
                {"name": "Docker",      "proficiency": 5},
                {"name": "Python",      "proficiency": 3},
                {"name": "CI/CD",       "proficiency": 4},
                {"name": "Prometheus",  "proficiency": 3},
            ],
        },
        {
            "id": "EMP-005", "name": "Elena Vasquez",
            "role": "Full-Stack Engineer", "department": "Product Engineering",
            "capacity_hours_per_week": 40.0, "hourly_rate_usd": 90.0,
            "skills": [
                {"name": "Python",     "proficiency": 4},
                {"name": "React",      "proficiency": 4},
                {"name": "FastAPI",    "proficiency": 3},
                {"name": "PostgreSQL", "proficiency": 3},
                {"name": "Docker",     "proficiency": 3},
                {"name": "TypeScript", "proficiency": 3},
            ],
        },
        {
            "id": "EMP-006", "name": "Ravi Nair",
            "role": "Data Engineer", "department": "Data Platform",
            "capacity_hours_per_week": 40.0, "hourly_rate_usd": 92.0,
            "skills": [
                {"name": "Python",        "proficiency": 5},
                {"name": "Apache Spark",  "proficiency": 4},
                {"name": "Kafka",         "proficiency": 5},
                {"name": "dbt",           "proficiency": 4},
                {"name": "AWS",           "proficiency": 4},
                {"name": "PostgreSQL",    "proficiency": 4},
                {"name": "Airflow",       "proficiency": 3},
            ],
        },
        {
            "id": "EMP-007", "name": "Priya Sharma",
            "role": "Senior Frontend Engineer", "department": "Product Engineering",
            "capacity_hours_per_week": 32.0, "hourly_rate_usd": 88.0,
            "skills": [
                {"name": "React",          "proficiency": 5},
                {"name": "TypeScript",     "proficiency": 4},
                {"name": "GraphQL",        "proficiency": 4},
                {"name": "Next.js",        "proficiency": 5},
                {"name": "CSS/Tailwind",   "proficiency": 4},
                {"name": "Testing (Jest)", "proficiency": 4},
            ],
        },
        {
            "id": "EMP-008", "name": "Tom Erikson",
            "role": "Backend Engineer", "department": "Platform Engineering",
            "capacity_hours_per_week": 40.0, "hourly_rate_usd": 80.0,
            "skills": [
                {"name": "Python",     "proficiency": 3},
                {"name": "FastAPI",    "proficiency": 3},
                {"name": "PostgreSQL", "proficiency": 3},
                {"name": "Docker",     "proficiency": 2},
                {"name": "AWS",        "proficiency": 2},
            ],
        },

        # ── Edge Case #1: Overlapping leave periods ────────────────────────
        # Alex has two leave records whose project-window portions overlap.
        # Bug #1 fix (interval merging) ensures we don't double-count.
        {
            "id": "EMP-009", "name": "Alex Morgan",
            "role": "Platform Engineer", "department": "Infrastructure",
            "capacity_hours_per_week": 40.0, "hourly_rate_usd": 87.0,
            "skills": [
                {"name": "Python",     "proficiency": 4},
                {"name": "AWS",        "proficiency": 4},
                {"name": "Docker",     "proficiency": 3},
                {"name": "Terraform",  "proficiency": 3},
                {"name": "CI/CD",      "proficiency": 3},
            ],
        },

        # ── Edge Case #2: Zero capacity (inactive / on LOA) ───────────────
        # Casey is on a leave of absence — contractual capacity is 0.
        # Bug #2 fix ensures they are hard-disqualified rather than given
        # availability_score=100 and slipping into the ranked list.
        {
            "id": "EMP-010", "name": "Casey Liu",
            "role": "Senior Data Scientist", "department": "AI/ML",
            "capacity_hours_per_week": 0.0,   # ← Zero capacity (LOA)
            "hourly_rate_usd": 105.0,
            "skills": [
                {"name": "Python",           "proficiency": 5},
                {"name": "Machine Learning", "proficiency": 5},
                {"name": "PyTorch",          "proficiency": 5},
                {"name": "AWS",              "proficiency": 4},
                {"name": "Apache Spark",     "proficiency": 4},
            ],
        },
    ],

    # ──────────────────────────────────────────────────────────────────────
    # ASSIGNMENTS
    # ──────────────────────────────────────────────────────────────────────
    "assignments": [
        # Aisha — 30 h/wk
        {
            "assignment_id": "ASGN-101", "employee_id": "EMP-001",
            "project_name": "Payments Gateway v2", "hours_per_week": 20.0,
            "start_date": _FMT(_TODAY - timedelta(weeks=4)),
            "end_date":   _FMT(_TODAY + timedelta(weeks=8)),
        },
        {
            "assignment_id": "ASGN-102", "employee_id": "EMP-001",
            "project_name": "Internal DevTools", "hours_per_week": 10.0,
            "start_date": _FMT(_TODAY - timedelta(weeks=2)),
            "end_date":   _FMT(_TODAY + timedelta(weeks=6)),
        },
        # Marcus — 40 h/wk (FULLY ALLOCATED)
        {
            "assignment_id": "ASGN-103", "employee_id": "EMP-002",
            "project_name": "LLM Inference Platform", "hours_per_week": 40.0,
            "start_date": _FMT(_TODAY - timedelta(weeks=8)),
            "end_date":   _FMT(_TODAY + timedelta(weeks=16)),
        },
        # Sofia — 20 h/wk
        {
            "assignment_id": "ASGN-104", "employee_id": "EMP-003",
            "project_name": "Customer Portal Redesign", "hours_per_week": 20.0,
            "start_date": _FMT(_TODAY - timedelta(weeks=3)),
            "end_date":   _FMT(_TODAY + timedelta(weeks=5)),
        },
        # Jamal — OVER-ALLOCATED: 48 h on 40 h capacity
        {
            "assignment_id": "ASGN-105", "employee_id": "EMP-004",
            "project_name": "K8s Cluster Migration", "hours_per_week": 30.0,
            "start_date": _FMT(_TODAY - timedelta(weeks=2)),
            "end_date":   _FMT(_TODAY + timedelta(weeks=6)),
        },
        {
            "assignment_id": "ASGN-106", "employee_id": "EMP-004",
            "project_name": "CI/CD Pipeline Overhaul", "hours_per_week": 18.0,
            "start_date": _FMT(_TODAY),
            "end_date":   _FMT(_TODAY + timedelta(weeks=4)),
        },
        # Elena — 16 h/wk
        {
            "assignment_id": "ASGN-107", "employee_id": "EMP-005",
            "project_name": "Analytics Dashboard", "hours_per_week": 16.0,
            "start_date": _FMT(_TODAY),
            "end_date":   _FMT(_TODAY + timedelta(weeks=4)),
        },
        # Ravi — 32 h/wk
        {
            "assignment_id": "ASGN-108", "employee_id": "EMP-006",
            "project_name": "Real-time Data Pipeline", "hours_per_week": 32.0,
            "start_date": _FMT(_TODAY - timedelta(weeks=3)),
            "end_date":   _FMT(_TODAY + timedelta(weeks=9)),
        },
        # Priya — 16 h/wk
        {
            "assignment_id": "ASGN-109", "employee_id": "EMP-007",
            "project_name": "Design System v3", "hours_per_week": 16.0,
            "start_date": _FMT(_TODAY),
            "end_date":   _FMT(_TODAY + timedelta(weeks=6)),
        },
        # Tom — no assignments
        # Alex (EMP-009) — 10 h/wk, leaving plenty of room
        {
            "assignment_id": "ASGN-110", "employee_id": "EMP-009",
            "project_name": "Cloud Cost Optimisation", "hours_per_week": 10.0,
            "start_date": _FMT(_TODAY),
            "end_date":   _FMT(_TODAY + timedelta(weeks=6)),
        },
        # Casey (EMP-010) — no assignments (on LOA)
    ],

    # ──────────────────────────────────────────────────────────────────────
    # LEAVE RECORDS
    # ──────────────────────────────────────────────────────────────────────
    "leaves": [
        # EMP-001 Aisha — ~20% overlap with typical 8-week project → warning
        {
            "leave_id": "LVE-201", "employee_id": "EMP-001",
            "leave_type": "PTO",
            "start_date": _FMT(_TODAY + timedelta(weeks=3)),
            "end_date":   _FMT(_TODAY + timedelta(weeks=3, days=10)),
        },
        # EMP-002 Marcus — 14-week parental leave (covers 100% of project window)
        {
            "leave_id": "LVE-202", "employee_id": "EMP-002",
            "leave_type": "Parental",
            "start_date": _FMT(_TODAY + timedelta(weeks=2)),
            "end_date":   _FMT(_TODAY + timedelta(weeks=16)),
        },
        # EMP-003 Sofia — short sick leave → partial warning
        {
            "leave_id": "LVE-203", "employee_id": "EMP-003",
            "leave_type": "Sick",
            "start_date": _FMT(_TODAY + timedelta(weeks=1)),
            "end_date":   _FMT(_TODAY + timedelta(weeks=1, days=4)),
        },
        # EMP-008 Tom — 8-day vacation → ~14% overlap warning
        {
            "leave_id": "LVE-204", "employee_id": "EMP-008",
            "leave_type": "PTO",
            "start_date": _FMT(_TODAY + timedelta(weeks=5)),
            "end_date":   _FMT(_TODAY + timedelta(weeks=5, days=7)),
        },

        # ── EMP-009 Alex: TWO overlapping leave records ───────────────────
        # Leave A covers project days 1-10 (10 days in window)
        # Leave B covers project days 5-18 (14 days in window, 6 overlap with A)
        # Unique overlap = days 1-18 = 18 days (NOT 10+14=24 days)
        # For an 8-week project (57 days) → 18/57 = 31.6% → PARTIAL warning
        {
            "leave_id": "LVE-205", "employee_id": "EMP-009",
            "leave_type": "PTO",
            "start_date": _FMT(_TODAY + timedelta(weeks=2)),          # = proj_start
            "end_date":   _FMT(_TODAY + timedelta(weeks=2, days=9)),  # proj days 1-10
        },
        {
            "leave_id": "LVE-206", "employee_id": "EMP-009",
            "leave_type": "Sick",
            "start_date": _FMT(_TODAY + timedelta(weeks=2, days=4)),  # proj day 5
            "end_date":   _FMT(_TODAY + timedelta(weeks=2, days=17)), # proj day 18
        },
    ],

    # ──────────────────────────────────────────────────────────────────────
    # BOUNTIES
    # ──────────────────────────────────────────────────────────────────────
    "bounties": [
        # EMP-001 Aisha
        {
            "bounty_id": "BNT-001", "employee_id": "EMP-001",
            "title": "Fix N+1 query in user reporting endpoint",
            "description": "Profiling revealed N+1 SQL issue.",
            "hours_estimated": 6.0, "status": "completed",
            "due_date":       _FMT(_TODAY - timedelta(days=20)),
            "completed_date": _FMT(_TODAY - timedelta(days=22)),
        },
        {
            "bounty_id": "BNT-002", "employee_id": "EMP-001",
            "title": "Add distributed tracing to Payments service",
            "description": "Instrument with OpenTelemetry.",
            "hours_estimated": 12.0, "status": "completed",
            "due_date":       _FMT(_TODAY - timedelta(days=10)),
            "completed_date": _FMT(_TODAY - timedelta(days=11)),
        },
        {
            "bounty_id": "BNT-003", "employee_id": "EMP-001",
            "title": "Upgrade PostgreSQL driver",
            "description": "Upgrade asyncpg 0.27 → 0.29.",
            "hours_estimated": 8.0, "status": "in_progress",
            "due_date": _FMT(_TODAY + timedelta(days=7)),
            "completed_date": None,
        },
        # EMP-002 Marcus — 2 overdue + 1 completed
        {
            "bounty_id": "BNT-004", "employee_id": "EMP-002",
            "title": "Benchmark GGUF vs ONNX inference latency",
            "description": "", "hours_estimated": 16.0, "status": "overdue",
            "due_date": _FMT(_TODAY - timedelta(days=14)), "completed_date": None,
        },
        {
            "bounty_id": "BNT-005", "employee_id": "EMP-002",
            "title": "Write LangChain tool integration tests",
            "description": "", "hours_estimated": 10.0, "status": "overdue",
            "due_date": _FMT(_TODAY - timedelta(days=7)), "completed_date": None,
        },
        {
            "bounty_id": "BNT-006", "employee_id": "EMP-002",
            "title": "Document model serving architecture",
            "description": "", "hours_estimated": 8.0, "status": "completed",
            "due_date":       _FMT(_TODAY - timedelta(days=30)),
            "completed_date": _FMT(_TODAY - timedelta(days=28)),
        },
        # EMP-003 Sofia — 1 in_progress past due (effectively overdue)
        {
            "bounty_id": "BNT-007", "employee_id": "EMP-003",
            "title": "Migrate Storybook to v8",
            "description": "", "hours_estimated": 14.0, "status": "in_progress",
            "due_date": _FMT(_TODAY - timedelta(days=5)), "completed_date": None,
        },
        {
            "bounty_id": "BNT-008", "employee_id": "EMP-003",
            "title": "Accessibility audit — WCAG 2.1 AA",
            "description": "", "hours_estimated": 10.0, "status": "completed",
            "due_date":       _FMT(_TODAY - timedelta(days=15)),
            "completed_date": _FMT(_TODAY - timedelta(days=14)),
        },
        # EMP-004 Jamal — overdue + completed
        {
            "bounty_id": "BNT-009", "employee_id": "EMP-004",
            "title": "Set up Prometheus alerting for K8s cluster",
            "description": "", "hours_estimated": 10.0, "status": "overdue",
            "due_date": _FMT(_TODAY - timedelta(days=3)), "completed_date": None,
        },
        {
            "bounty_id": "BNT-010", "employee_id": "EMP-004",
            "title": "Terraform module for VPC peering",
            "description": "", "hours_estimated": 8.0, "status": "completed",
            "due_date":       _FMT(_TODAY - timedelta(days=20)),
            "completed_date": _FMT(_TODAY - timedelta(days=18)),
        },
        # EMP-005 Elena — all completed (perfect reliability)
        {
            "bounty_id": "BNT-011", "employee_id": "EMP-005",
            "title": "CSV export for analytics dashboard",
            "description": "", "hours_estimated": 8.0, "status": "completed",
            "due_date":       _FMT(_TODAY - timedelta(days=10)),
            "completed_date": _FMT(_TODAY - timedelta(days=12)),
        },
        {
            "bounty_id": "BNT-012", "employee_id": "EMP-005",
            "title": "Integrate Sentry error tracking",
            "description": "", "hours_estimated": 5.0, "status": "completed",
            "due_date":       _FMT(_TODAY - timedelta(days=5)),
            "completed_date": _FMT(_TODAY - timedelta(days=6)),
        },
        {
            "bounty_id": "BNT-013", "employee_id": "EMP-005",
            "title": "E2E tests for checkout flow",
            "description": "", "hours_estimated": 10.0, "status": "completed",
            "due_date":       _FMT(_TODAY - timedelta(days=2)),
            "completed_date": _FMT(_TODAY - timedelta(days=3)),
        },
        # EMP-006 Ravi — 1 completed, 1 not_started (future due → neutral)
        {
            "bounty_id": "BNT-014", "employee_id": "EMP-006",
            "title": "Optimise Spark shuffle partitions",
            "description": "", "hours_estimated": 12.0, "status": "completed",
            "due_date":       _FMT(_TODAY - timedelta(days=8)),
            "completed_date": _FMT(_TODAY - timedelta(days=7)),
        },
        {
            "bounty_id": "BNT-015", "employee_id": "EMP-006",
            "title": "Add dbt data quality tests",
            "description": "", "hours_estimated": 9.0, "status": "not_started",
            "due_date": _FMT(_TODAY + timedelta(days=10)),  # not yet due
            "completed_date": None,
        },
        # EMP-007 Priya — 2 in_progress (future due → no penalty, but active drain)
        {
            "bounty_id": "BNT-016", "employee_id": "EMP-007",
            "title": "Build token usage dashboard",
            "description": "", "hours_estimated": 10.0, "status": "in_progress",
            "due_date": _FMT(_TODAY + timedelta(days=5)), "completed_date": None,
        },
        {
            "bounty_id": "BNT-017", "employee_id": "EMP-007",
            "title": "Add dark-mode support to component library",
            "description": "", "hours_estimated": 8.0, "status": "in_progress",
            "due_date": _FMT(_TODAY + timedelta(days=14)), "completed_date": None,
        },
        # EMP-008 Tom — NO bounties (neutral reliability)
        # EMP-009 Alex — 1 completed, 1 not_started (future due) → healthy
        {
            "bounty_id": "BNT-018", "employee_id": "EMP-009",
            "title": "Write Terraform module for S3 lifecycle policies",
            "description": "", "hours_estimated": 6.0, "status": "completed",
            "due_date":       _FMT(_TODAY - timedelta(days=7)),
            "completed_date": _FMT(_TODAY - timedelta(days=6)),
        },
        {
            "bounty_id": "BNT-019", "employee_id": "EMP-009",
            "title": "Document AWS cost tagging standards",
            "description": "", "hours_estimated": 4.0, "status": "not_started",
            "due_date": _FMT(_TODAY + timedelta(days=14)),  # future → no penalty
            "completed_date": None,
        },
        # EMP-010 Casey — bounties assigned but LOA means nothing actionable
        {
            "bounty_id": "BNT-020", "employee_id": "EMP-010",
            "title": "Prototype LLM fine-tuning pipeline",
            "description": "", "hours_estimated": 20.0, "status": "not_started",
            "due_date": _FMT(_TODAY + timedelta(days=30)), "completed_date": None,
        },
    ],
}