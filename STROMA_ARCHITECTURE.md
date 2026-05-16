# STROMA — Full Architecture & Solution Design
**Staff & Organisational Resource Management Agent**  
*Agent 4 in the IRIS → CELL → CORTEX → STROMA pipeline*  
*Prepared for OpenCode implementation*

---

## 0. Pipeline Context

```
Meeting artifacts (R2)
        │
        ▼
    IRIS (8000)          — Agent 1: Extracts structured insights.yaml per meeting
        │  NERVE event
        ▼
    CELL (8002)          — Agent 2: Intern tasks, EOD, bounties, ERP writes
        │  Weekly summary API
        ▼
    CORTEX (8004)        — Agent 3: Project memory, health, documents, PM intelligence
        │
        ▼
    STROMA (8006)        — Agent 4: People lifecycle, department capacity, org health
        │
        ▼
    Intranet HRMS page   — Dept head / HR / CEO chatbot + push alerts
```

**IRIS perceives. CELL executes. CORTEX thinks. STROMA sustains.**

---

## 1. What STROMA Is

STROMA is the people and organisational intelligence layer for the consultancy. It tracks every person below the CEO through their full lifecycle — from the day an intern joins to the day they leave or get converted. It synthesises signals from CELL (task performance, bounty velocity, EOD compliance), the assessment system (monthly structured tests), and the intranet HRMS (role, department, skills) into a living picture of each person's growth trajectory.

STROMA runs mostly on a **monthly cadence** — not real-time. People data is slower-moving than project data. It processes incrementally and pushes time-sensitive flags (hiring gaps, conversion deadlines, leech warnings) proactively. Everything else is queryable via a role-aware chatbot on the HRMS intranet page.

It is the agent that notices someone is being extended past their productive window before anyone else does. It is the agent that tells leadership to start hiring 10 days before a capacity gap hits. It is the agent that recommends a conversion before the founder has to ask.

---

## 2. Full Capability Set

### Lifecycle Tracking
- Own the full intern lifecycle: join → 2-2-2 progression → graduate/extend/convert/leave
- Track current stage for every person (intern-bounty | intern-hybrid | full-time | apm)
- Flag upcoming stage gate decisions at the right time (not after the deadline)
- Detect and flag extended interns with low conversion likelihood — "leech signal"
- Recommend let-go vs extend vs convert at 6-month mark

### Performance Intelligence
- Monthly performance snapshot per person — pulled from CELL, assessment system, HRMS
- Bounty velocity as universal work-tracking signal (interpreted by stage)
- EOD compliance rate as accountability signal
- Task completion rate, carry rate, block rate from CELL weekly summaries
- Assessment scores from monthly company tests (structured output ingested automatically)
- Produce a growth score per person — deterministic, not LLM

### Hiring Intelligence
- Track department bench capacity (active headcount vs project load)
- Flag hiring need before the gap hits — account for 7–10 day hiring window
- Know intern graduation pipeline — who is leaving at 6 months, when, how many slots open
- Recommend batch size for next hiring round based on projected capacity
- Notify HR and dept head when to initiate hiring

### Compensation Awareness
- Know which stage each person is on and what that means for cost
- Intern (bounty-paid): cost = bounty units × ₹100
- Hybrid: fixed stipend ₹4-5k + bounties tracked (not paid)
- Full-time: fixed ₹8-10k + bounties tracked (not paid)
- APM: fixed (dept head configures)
- Report cost per person, per department, per project (project cost = sum of allocated people cost)

### Skill Profile
- Maintain a skill profile per person — tech stack, reasoning, engineering capability
- Pull baseline from intranet HRMS (existing tags, cleaned up)
- Update monthly from assessment system structured output
- Produce a skill delta over time — growth visible as trajectory, not just snapshot

### Chatbot — Role-Aware HRMS Interface
- Embedded on intranet HRMS page
- Employee identity resolved from intranet session (same pattern as CORTEX)
- Dept heads see their department people and capacity
- HR sees org-wide lifecycle, compensation, hiring pipeline
- CEO sees org health summary, cost, headcount, conversion pipeline

---

## 3. The Intern Lifecycle — Core State Machine

```
JOINED
  └── stage: intern_bounty
  └── pay: bounty × ₹100 (real money)
  └── duration: 0–2 months
        │
        ▼ 2-month gate — dept head or tech lead approves progression
        │
   ┌────┴──────────────────────────────┐
   │ progressing                       │ not progressing
   ▼                                   ▼
intern_hybrid                     extend OR let_go (human decision, STROMA recommends)
  └── pay: fixed ₹4-5k + tracked bounties
  └── duration: 2–4 months
        │
        ▼ 2-month gate
        │
   ┌────┴──────────────────────────────┐
   │ progressing                       │ not progressing
   ▼                                   ▼
full_time                         extend OR let_go (STROMA recommends)
  └── pay: fixed ₹8-10k + tracked bounties
  └── duration: 4–6 months
        │
        ▼ 6-month gate — performance + founders office decision
        │
   ┌────┴──────────────────┬───────────────────────┐
   │                       │                       │
   ▼                       ▼                       ▼
convert_to_apm        extend internship        let_go
  └── founders office    └── STROMA flags:      └── STROMA flags:
      stress test (2mo)      leech risk if          recommend at
      → CEO decision         low trajectory         6-month mark
```

### Stage Gate Rules
- Gate decisions are **human-made** — dept head or tech lead approves
- STROMA **recommends** based on growth score and trajectory at each gate
- STROMA fires a flag **14 days before** each gate deadline to give the approver time
- If no decision is recorded within 7 days of gate deadline, STROMA escalates to dept head

### The Leech Signal
An extended intern costs more than a fresh hire and occupies a headcount slot.
STROMA fires a leech warning when:
- Intern has been extended past 6 months AND
- Growth score trajectory is flat or declining over last 2 months AND
- No conversion process has been initiated

Warning goes to dept head and HR.

---

## 4. Growth Score Algorithm

Deterministic. Not LLM. Same philosophy as CORTEX health score — auditable, testable, no hallucination risk on a number people act on.

Computed **monthly** per person. Stored as a time series so trajectory is visible.

```python
def compute_growth_score(
    cell_summary: dict,        # last 4 weeks of CELL data for this person
    assessment: dict,          # latest monthly assessment result
    eod_log: dict,             # EOD compliance last 30 days
    stage: str,                # current lifecycle stage
) -> dict:
    score = 100
    components = {}

    # ── Task completion rate (from CELL) ──────────────────────────
    completion_rate = cell_summary.get("completion_rate", 0)
    completion_penalty = 0
    if completion_rate < 0.5:
        completion_penalty = 25
    elif completion_rate < 0.7:
        completion_penalty = 12
    elif completion_rate < 0.85:
        completion_penalty = 5
    score -= completion_penalty
    components["completion_penalty"] = completion_penalty

    # ── Carry rate (tasks not done, rolled over) ──────────────────
    carry_rate = cell_summary.get("carry_rate", 0)
    carry_penalty = 0
    if carry_rate > 0.4:
        carry_penalty = 15
    elif carry_rate > 0.2:
        carry_penalty = 7
    score -= carry_penalty
    components["carry_penalty"] = carry_penalty

    # ── EOD compliance ────────────────────────────────────────────
    eod_compliance = eod_log.get("compliance_rate", 1.0)
    eod_penalty = 0
    if eod_compliance < 0.6:
        eod_penalty = 20
    elif eod_compliance < 0.8:
        eod_penalty = 10
    score -= eod_penalty
    components["eod_penalty"] = eod_penalty

    # ── Assessment score (monthly test) ──────────────────────────
    # Assessment system returns 0–100 score
    assessment_score = assessment.get("score", 50)
    assessment_penalty = 0
    if assessment_score < 40:
        assessment_penalty = 20
    elif assessment_score < 60:
        assessment_penalty = 10
    elif assessment_score >= 80:
        assessment_penalty = -10  # bonus
    score -= assessment_penalty
    components["assessment_penalty"] = assessment_penalty

    # ── Bounty velocity (work output signal, not pay) ─────────────
    # Normalised against stage peers — not absolute
    bounty_percentile = cell_summary.get("bounty_percentile_in_stage", 0.5)
    velocity_penalty = 0
    if bounty_percentile < 0.25:
        velocity_penalty = 15
    elif bounty_percentile < 0.4:
        velocity_penalty = 7
    score -= velocity_penalty
    components["velocity_penalty"] = velocity_penalty

    final = max(0, min(100, score))
    band = "strong" if final >= 80 else "developing" if final >= 60 else "at_risk"
    return {"score": final, "band": band, "components": components}
```

### Growth Trajectory
After each monthly computation, STROMA computes trajectory from the last 3 snapshots:
- 3 consecutive increases → `improving`
- Within ±5 points → `stable`
- 2+ consecutive declines → `declining`

Trajectory feeds: stage gate recommendations, leech signal, conversion recommendations.

---

## 5. Department Capacity Model

```
department
  └── active_headcount      — employees with active = true
  └── project_load          — sum of project_member allocations (from ERP)
  └── upcoming_exits        — interns at 6-month mark in next 30/60 days
  └── capacity_score        — headcount / projected_need
```

### Hiring Flag Logic

```python
def should_flag_hiring(dept: Department) -> HiringFlag | None:
    # Count interns graduating in next 45 days
    graduating_soon = count_interns_graduating_within(dept.id, days=45)

    # Count current active interns below full_time stage
    active_early_interns = count_by_stage(dept.id, ["intern_bounty", "intern_hybrid"])

    # Project need: rough heuristic — 1 intern per active project in dept
    projected_need = count_active_projects_in_dept(dept.id)

    # Bench after graduations
    bench_after = (active_early_interns - graduating_soon)

    if bench_after < projected_need * 0.7:
        shortfall = projected_need - bench_after
        return HiringFlag(
            department_id=dept.id,
            shortfall=shortfall,
            graduating_soon=graduating_soon,
            recommended_batch_size=shortfall + 1,  # +1 buffer
            hire_by_date=earliest_graduation_date - timedelta(days=10),
            urgency="high" if bench_after < projected_need * 0.5 else "medium"
        )
    return None
```

Hiring flags go to HR and dept head via Slack DM (same delivery pattern as CELL/CORTEX).

---

## 6. API Endpoints

### `POST /stroma/sync-cell`

Called by the monthly job. Pulls the last 4 weeks of CELL per-intern summaries for all active employees. Returns `202 Accepted`.

**Request body:**
```json
{
  "week_refs": ["2025-W17", "2025-W18", "2025-W19", "2025-W20"]
}
```

---

### `POST /stroma/sync-assessment`

Receive monthly assessment results from the assessment system. Triggers growth score recomputation for affected employees.

**Request body:**
```json
{
  "assessment_date": "2025-05-01",
  "results": [
    {
      "employee_id": "p-arjun-001",
      "score": 74,
      "components": {
        "technical": 80,
        "reasoning": 70,
        "communication": 72
      },
      "notes": "Strong on system design, weak on estimation"
    }
  ]
}
```

**Response `202`:**
```json
{ "status": "accepted", "employees_queued": 12 }
```

---

### `POST /stroma/stage-transition`

Record a stage gate decision. Called by intranet when dept head or tech lead approves/denies a progression.

**Request body:**
```json
{
  "employee_id": "p-arjun-001",
  "from_stage": "intern_bounty",
  "to_stage": "intern_hybrid",
  "decision": "progress",
  "decided_by": "p-head-001",
  "effective_date": "2025-05-14",
  "notes": "Strong completion rate, good communication improvement"
}
```

`decision` values: `progress` | `extend` | `let_go` | `convert_to_apm`

**Response `200`:**
```json
{ "status": "recorded", "employee_id": "p-arjun-001", "new_stage": "intern_hybrid" }
```

---

### `POST /stroma/employee-join`

Register a new employee. Called by intranet/HRMS when HR onboards someone.

**Request body:**
```json
{
  "employee_id": "p-new-001",
  "name": "Priya Sharma",
  "department_id": "dept-eng-001",
  "role": "intern",
  "slack_user_id": "U09NEWSLACK",
  "join_date": "2025-05-14",
  "initial_skills": ["python", "react"]
}
```

---

### `POST /stroma/chat`

Role-aware HRMS chatbot. Called from intranet HRMS page.

**Request headers:** `X-Employee-ID`, `X-Department-ID` (optional — scopes to dept if provided)

**Request body:**
```json
{
  "message": "Who is at risk of churning this month?",
  "conversation_history": []
}
```

**Response:**
```json
{
  "response": "...",
  "sources": ["employee_id:p-arjun-001", "dept:Engineering"],
  "generated_at": "2025-05-14T09:00:00Z"
}
```

---

### `GET /stroma/department/{department_id}/snapshot`

Return full department health snapshot. Used by intranet dashboard.

```json
{
  "department_id": "dept-eng-001",
  "department_name": "Engineering",
  "headcount": 8,
  "by_stage": {
    "intern_bounty": 2,
    "intern_hybrid": 3,
    "full_time": 2,
    "apm": 1
  },
  "capacity_score": 0.85,
  "hiring_flag": null,
  "at_risk_employees": 1,
  "graduating_in_30_days": 2,
  "generated_at": "2025-05-14T09:00:00Z"
}
```

---

### `GET /stroma/employee/{employee_id}/profile`

Full people profile for a single employee. Admin/HR use.

```json
{
  "employee_id": "p-arjun-001",
  "name": "Arjun Sharma",
  "stage": "intern_hybrid",
  "join_date": "2025-03-14",
  "stage_start_date": "2025-05-14",
  "next_gate_date": "2025-07-14",
  "growth_score": 74,
  "growth_band": "developing",
  "growth_trajectory": "improving",
  "skills": ["python", "fastapi", "react", "system_design"],
  "monthly_snapshots": [...],
  "stage_history": [...],
  "current_projects": ["PROJ-CRM-0014"],
  "total_bounty_units": 48.5,
  "leech_flag": false
}
```

---

### `GET /stroma/health`

```json
{ "status": "ok", "agent": "STROMA" }
```

---

## 7. Database Schema (Postgres)

```sql
-- ─────────────────────────────────────────
-- PEOPLE
-- ─────────────────────────────────────────

CREATE TABLE departments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  erp_department_id TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  head_employee_id TEXT,            -- dept head employee_id
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE people (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id TEXT UNIQUE NOT NULL,         -- e.g. p-arjun-001, matches CELL employees table
  name TEXT NOT NULL,
  slack_user_id TEXT,
  department_id UUID REFERENCES departments(id),
  current_stage TEXT CHECK (current_stage IN (
    'intern_bounty', 'intern_hybrid', 'full_time', 'apm', 'tech_lead', 'pm', 'dept_head'
  )) NOT NULL,
  join_date DATE NOT NULL,
  stage_start_date DATE NOT NULL,
  active BOOLEAN DEFAULT TRUE,
  exit_date DATE,
  exit_reason TEXT CHECK (exit_reason IN (
    'graduated_let_go', 'voluntary_exit', 'converted', 'extended'
  )),
  outside_hire BOOLEAN DEFAULT FALSE,       -- exception flag (e.g. sales full-timer)
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────
-- LIFECYCLE
-- ─────────────────────────────────────────

CREATE TABLE stage_transitions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id TEXT NOT NULL,
  from_stage TEXT NOT NULL,
  to_stage TEXT NOT NULL,
  decision TEXT CHECK (decision IN ('progress', 'extend', 'let_go', 'convert_to_apm')) NOT NULL,
  decided_by TEXT NOT NULL,                 -- employee_id of approver
  effective_date DATE NOT NULL,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE stage_gate_flags (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id TEXT NOT NULL,
  gate_due_date DATE NOT NULL,
  from_stage TEXT NOT NULL,
  flag_sent_at TIMESTAMPTZ,
  escalated_at TIMESTAMPTZ,
  resolved BOOLEAN DEFAULT FALSE,
  resolved_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────
-- PERFORMANCE
-- ─────────────────────────────────────────

CREATE TABLE growth_snapshots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id TEXT NOT NULL,
  snapshot_month DATE NOT NULL,             -- first day of month, e.g. 2025-05-01
  growth_score INTEGER NOT NULL CHECK (growth_score BETWEEN 0 AND 100),
  growth_band TEXT CHECK (growth_band IN ('strong', 'developing', 'at_risk')) NOT NULL,
  growth_trajectory TEXT CHECK (growth_trajectory IN ('improving', 'stable', 'declining')),
  components JSONB NOT NULL,
  -- {completion_penalty, carry_penalty, eod_penalty, assessment_penalty, velocity_penalty}
  cell_data JSONB,                          -- raw CELL summary used for this snapshot
  assessment_data JSONB,                    -- raw assessment data used
  computed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE assessment_results (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id TEXT NOT NULL,
  assessment_date DATE NOT NULL,
  score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
  components JSONB,
  -- {technical, reasoning, communication, ...}
  notes TEXT,
  ingested_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────
-- SKILLS
-- ─────────────────────────────────────────

CREATE TABLE skill_profiles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id TEXT UNIQUE NOT NULL,
  skills JSONB NOT NULL DEFAULT '[]',
  -- [{name, category, proficiency: "beginner|competent|strong", last_updated}]
  last_updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE skill_history (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id TEXT NOT NULL,
  snapshot_date DATE NOT NULL,
  skills JSONB NOT NULL,
  source TEXT CHECK (source IN ('assessment', 'hrms_manual', 'stroma_inferred')) NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────
-- COMPENSATION
-- ─────────────────────────────────────────

CREATE TABLE compensation_records (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  effective_from DATE NOT NULL,
  pay_type TEXT CHECK (pay_type IN ('bounty_only', 'fixed_stipend', 'fixed_salary')) NOT NULL,
  fixed_amount NUMERIC(10,2),               -- monthly INR, null for bounty_only
  bounty_rate NUMERIC(5,2) DEFAULT 100.0,   -- INR per bounty unit (default 100)
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────
-- DEPARTMENT CAPACITY
-- ─────────────────────────────────────────

CREATE TABLE capacity_snapshots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  department_id UUID REFERENCES departments(id),
  snapshot_date DATE NOT NULL,
  headcount INTEGER NOT NULL,
  by_stage JSONB NOT NULL,
  -- {intern_bounty: N, intern_hybrid: N, full_time: N, apm: N}
  capacity_score NUMERIC(4,3),
  computed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE hiring_flags (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  department_id UUID REFERENCES departments(id),
  shortfall INTEGER NOT NULL,
  graduating_soon INTEGER NOT NULL,
  recommended_batch_size INTEGER NOT NULL,
  hire_by_date DATE NOT NULL,
  urgency TEXT CHECK (urgency IN ('high', 'medium', 'low')) NOT NULL,
  status TEXT CHECK (status IN ('open', 'acknowledged', 'resolved')) DEFAULT 'open',
  flagged_at TIMESTAMPTZ DEFAULT NOW(),
  resolved_at TIMESTAMPTZ
);

-- ─────────────────────────────────────────
-- LEECH FLAGS
-- ─────────────────────────────────────────

CREATE TABLE leech_flags (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id TEXT NOT NULL,
  flagged_at TIMESTAMPTZ DEFAULT NOW(),
  months_extended INTEGER NOT NULL,
  growth_trajectory TEXT NOT NULL,
  recommendation TEXT CHECK (recommendation IN ('let_go', 'final_warning', 'monitor')) NOT NULL,
  status TEXT CHECK (status IN ('open', 'acknowledged', 'resolved')) DEFAULT 'open',
  resolved_at TIMESTAMPTZ,
  resolved_by TEXT
);

-- ─────────────────────────────────────────
-- AUDIT
-- ─────────────────────────────────────────

CREATE TABLE stroma_actions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id TEXT,
  department_id UUID,
  action_type TEXT NOT NULL,
  -- monthly_snapshot | stage_gate_flag | hiring_flag | leech_flag | assessment_ingested
  -- stage_transition | growth_score_computed | slack_alert_sent
  payload JSONB,
  triggered_by TEXT,
  -- schedule | webhook | intranet_api | slack_command
  status TEXT DEFAULT 'ok',
  error_text TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 8. Monthly Processing Pipeline

STROMA runs its core intelligence job once per month. Heavy LLM usage is avoided — most processing is deterministic. LLM is used only for the chatbot and for free-text recommendation notes.

```
Monthly job fires (1st of each month, 06:00 IST)
            │
            ▼
[1] PULL CELL DATA
    For each active employee:
      GET /cell/summary/{project_id}?employee_id=...&weeks=last4
      Aggregate: completion_rate, carry_rate, bounty_units, eod_compliance
      Compute bounty_percentile_in_stage (rank within same-stage peers)

            │
            ▼
[2] PULL ASSESSMENT DATA
    If assessment_results received this month for this employee:
      Use latest result
    Else:
      Use last available (flag as stale if > 45 days old)

            │
            ▼
[3] COMPUTE GROWTH SCORE
    For each active employee:
      compute_growth_score(cell_data, assessment, eod_log, stage)
      Compute trajectory from last 3 snapshots
      Insert growth_snapshots row

            │
            ▼
[4] STAGE GATE CHECK
    For each employee:
      Compute days since stage_start_date
      If gate approaching within 14 days AND no flag sent:
        Insert stage_gate_flags row
        DM dept head / tech lead: "Gate decision due for {name} in {N} days"
      If gate past due AND unresolved:
        Escalate to dept head

            │
            ▼
[5] LEECH DETECTION
    For each intern past 6 months with no conversion initiated:
      If growth_trajectory in ('stable', 'declining'):
        Insert leech_flags row
        DM dept head + HR

            │
            ▼
[6] DEPARTMENT CAPACITY SCAN
    For each department:
      Count active people by stage
      Count interns graduating in next 45 days
      Run should_flag_hiring()
      If hiring flag:
        Insert hiring_flags row
        DM dept head + HR

            │
            ▼
[7] SKILL PROFILE UPDATE
    For each employee with new assessment_results this month:
      Merge assessment skill components into skill_profiles
      Insert skill_history snapshot
      Write back to intranet HRMS (PATCH /api/employees/:id/skills)

            │
            ▼
[8] CAPACITY SNAPSHOT
    For each department:
      Insert capacity_snapshots row
```

---

## 9. Scheduled Jobs (IST)

| Time (IST) | Job | What it does |
|---|---|---|
| 1st of month, 06:00 | `monthly_job` | Full pipeline: CELL pull → growth scores → stage gates → leech detection → capacity scan → skill update |
| Daily 09:00 | `gate_deadline_monitor` | Check for any stage gate flags overdue by > 7 days → escalate |
| Daily 09:00 | `hiring_flag_monitor` | Re-check open hiring flags — escalate if hire_by_date approaching within 5 days |
| Weekly Mon 08:00 | `capacity_summary_job` | Send dept capacity summary to dept heads (lightweight — no recomputation, just current snapshot) |

All jobs: APScheduler `AsyncIOScheduler`, `timezone=Asia/Kolkata`.
Use `lifespan` context manager (not deprecated `@app.on_event`).

---

## 10. Slack Message Formats

### Stage Gate Flag (to dept head / tech lead)
```
STROMA — Stage Gate Due in 14 Days

Arjun Sharma (p-arjun-001) is approaching their intern_bounty → intern_hybrid gate.
Gate date: 28 May 2025

Current indicators:
  Growth score: 74/100 (developing, improving)
  Completion rate: 82% (last 4 weeks)
  EOD compliance: 91%
  Assessment: 74/100 (May)

STROMA recommendation: Progress to intern_hybrid
This is based on improving trajectory and above-threshold completion rate.

Please record your decision:
  Reply: progress / extend / let_go
  Or log on the intranet HRMS page.
```

### Leech Warning (to dept head + HR)
```
STROMA — Extended Intern Warning

Priya Dev (p-priya-002) has been on internship for 7.5 months.
Stage: full_time (extended past standard 6-month window)
No conversion process initiated.

Growth trajectory: stable (not improving for 2 months)
Growth score: 58/100 (developing)

STROMA recommendation: Let go — trajectory does not support conversion.
Cost to date: 2.5 months extra fixed pay + tracked bounties.
A fresh intern batch can be onboarded within 10 days.

Please action on the HRMS page.
```

### Hiring Flag (to dept head + HR)
```
STROMA — Hiring Needed: Engineering

2 interns graduating in the next 30 days:
  p-arjun-001 (Arjun Sharma) — gate: 14 Jun
  p-ritu-003 (Ritu Singh) — gate: 21 Jun

Current bench after exits: 2 active early-stage interns
Active projects needing coverage: 4

Recommended batch size: 3 interns
You need to initiate hiring by: 4 Jun 2025
(Accounts for 10-day hiring window)

Please initiate on the HRMS page or reply: acknowledged
```

### Weekly Capacity Summary (to dept heads, Monday)
```
STROMA — Engineering Capacity — Week of 19 May 2025

Headcount: 8 active
  intern_bounty : 2
  intern_hybrid : 3
  full_time     : 2
  apm           : 1

Upcoming gates (next 30 days): 2
Hiring flag    : None
At-risk        : 1 (Priya Dev — leech flag open)

Full department view: https://intranet.internal/hrms/dept/engineering
```

---

## 11. Role-Aware Chatbot

### Architecture

```
Intranet HRMS page
    │  POST /stroma/chat
    │  Headers: X-Employee-ID (from intranet session)
    ▼
STROMA /chat endpoint
    │
    ├── Resolve employee role from intranet API
    ├── If dept_head: scope to their department
    ├── If hr: org-wide access
    ├── If ceo: org-wide + cost summary
    ├── Load relevant people profiles, growth snapshots, capacity data
    ├── Build role-aware system prompt
    └── LLM call → response
```

### Role-Aware System Prompts

```python
ROLE_SYSTEM_PROMPTS = {
    "dept_head": """You are STROMA, a people intelligence assistant for a department head
    at a software consultancy. The dept head manages intern lifecycles and team capacity.
    Be direct — surface names, scores, flags. Recommend actions. Reference dates and stages.""",

    "hr": """You are STROMA, a people intelligence assistant for HR.
    HR needs org-wide lifecycle visibility, hiring pipeline status, compensation
    summaries, and flag resolution. Be precise and factual.""",

    "ceo": """You are STROMA. Give high-level org health — headcount by stage,
    conversion pipeline, cost summary, open hiring flags, at-risk people.
    No operational detail. Business impact framing.""",

    "default": """You are STROMA, a people intelligence assistant.
    Answer accurately based on people and organisation data."""
}
```

### Example Queries by Role

**Dept head:** "Who needs a stage decision this month?"
→ Lists employees with upcoming gates, growth scores, STROMA recommendation per person

**HR:** "How many interns are we graduating in June?"
→ Counts by department, flags which have conversion potential vs likely exits

**CEO:** "What does our intern pipeline look like?"
→ "18 active interns across 4 departments. 4 graduating in June. 1 conversion candidate in founders office pipeline. 2 hiring flags open (Engineering, Product). Monthly intern cost: ₹X."

---

## 12. Integration Wires

| From | To | Mechanism | Notes |
|---|---|---|---|
| CELL | STROMA | `GET /cell/summary` per employee | Monthly pull — STROMA calls CELL |
| Assessment system | STROMA | `POST /stroma/sync-assessment` | Assessment system pushes after each monthly test |
| Intranet HRMS | STROMA | `POST /stroma/employee-join` | On new hire onboarding |
| Intranet HRMS | STROMA | `POST /stroma/stage-transition` | On dept head gate decision |
| Intranet | STROMA | `POST /stroma/chat` | HRMS page chatbot |
| STROMA | Intranet HRMS | `PATCH /api/employees/:id/skills` | Write back updated skill profile monthly |
| STROMA | Slack | Slack SDK | Stage gate flags, leech warnings, hiring flags, weekly digest |
| ERP | STROMA | `GET /api/project-members` | Read project allocations for capacity model |

---

## 13. New Endpoint CELL Must Implement

STROMA needs per-person weekly summaries. CELL's existing `/cell/summary/{project_id}` is project-scoped. STROMA needs employee-scoped data.

```http
GET /cell/employee-summary/{employee_id}?weeks=last4
X-Api-Key: <cell-api-key>

Response 200:
{
  "employee_id": "p-arjun-001",
  "weeks": ["2025-W17", "2025-W18", "2025-W19", "2025-W20"],
  "tasks_planned": 24,
  "tasks_completed": 18,
  "tasks_carried": 4,
  "tasks_blocked": 2,
  "completion_rate": 0.75,
  "carry_rate": 0.17,
  "eod_compliance_rate": 0.90,
  "total_bounty_units": 14.5,
  "per_week": [
    {
      "week_ref": "2025-W20",
      "tasks_planned": 6,
      "tasks_completed": 5,
      "tasks_carried": 1,
      "tasks_blocked": 0,
      "eod_submitted": true,
      "bounty_units": 4.0
    }
  ]
}
```

---

## 14. FastAPI Service Structure

```
stroma/
├── main.py                        # FastAPI app, scheduler init, lifespan context manager
├── config.py                      # env vars, constants, thresholds
│
├── models/
│   ├── db.py                      # asyncpg models matching §7 schema
│   └── schemas.py                 # Pydantic v2 schemas
│
├── routers/
│   ├── sync.py                    # POST /stroma/sync-cell, POST /stroma/sync-assessment
│   ├── lifecycle.py               # POST /stroma/stage-transition, POST /stroma/employee-join
│   ├── chat.py                    # POST /stroma/chat
│   ├── snapshots.py               # GET /stroma/department/:id/snapshot, GET /stroma/employee/:id/profile
│   └── slack.py                   # Slack interactive payloads (gate decisions via reply)
│
├── services/
│   ├── cell_pull.py               # Pull CELL employee summaries for monthly job
│   ├── growth.py                  # compute_growth_score(), trajectory calculation
│   ├── stage_gates.py             # Gate deadline detection, escalation logic
│   ├── leech.py                   # Leech flag detection logic
│   ├── capacity.py                # Department capacity model, hiring flag logic
│   ├── skills.py                  # Skill profile merge, HRMS write-back
│   └── monthly_job.py             # Orchestrates full monthly pipeline steps 1–8
│
├── chat/
│   ├── engine.py                  # Chat orchestration: role resolution, context load, LLM call
│   └── role_prompts.py            # Role-aware system prompts
│
├── llm/
│   └── client.py                  # Anthropic API wrapper (same pattern as CORTEX)
│
├── integrations/
│   ├── cell_api.py                # GET /cell/employee-summary
│   ├── intranet.py                # Employee/role lookup, skill write-back
│   ├── erp.py                     # GET /api/project-members
│   └── slack.py                   # Slack DM delivery
│
└── scheduler.py                   # APScheduler AsyncIOScheduler, IST-anchored
```

---

## 15. Environment Variables (Production)

```env
# Database
DATABASE_URL=postgresql+asyncpg://stroma_user:stroma_pass@<host>:5432/stroma_db

# LLM (chatbot only — no LLM in scoring pipeline)
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-20250514

# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...

# Internal services
CELL_API_BASE_URL=https://cell.internal
CELL_API_KEY=...
ERP_BASE_URL=https://erp.internal
ERP_API_KEY=...
INTRANET_API_BASE_URL=https://intranet.internal/api
INTRANET_API_KEY=...

# STROMA service
STROMA_HOST=0.0.0.0
STROMA_PORT=8006
STROMA_API_KEY=...

# Timezone
TZ=Asia/Kolkata

# Thresholds
STAGE_GATE_WARNING_DAYS=14
STAGE_GATE_ESCALATION_DAYS=7
LEECH_DETECTION_MONTHS=6
HIRING_WINDOW_DAYS=10
HIRING_LOOKAHEAD_DAYS=45
CAPACITY_ALERT_THRESHOLD=0.7
```

---

## 16. Production Readiness Gaps

| # | Issue | Severity | Fix |
|---|---|---|---|
| 1 | **CELL `/cell/employee-summary` endpoint does not exist** | Critical | CELL team must implement per §13 |
| 2 | **`people` table must be seeded from ERP/HRMS on deploy** | Critical | All active employees must be imported with correct stage and join_date before first monthly job |
| 3 | **Assessment system integration** | High | Confirm assessment system can POST structured results to `/stroma/sync-assessment` after each monthly test. Agree on `employee_id` as the join key |
| 4 | **Intranet HRMS skill write-back endpoint** | High | Intranet must expose `PATCH /api/employees/:id/skills` for STROMA to update profiles |
| 5 | **No authentication on `/stroma/` endpoints** | High | Add `X-API-Key` FastAPI dependency on all routes |
| 6 | **`bounty_percentile_in_stage` requires peers** | Medium | Growth score percentile ranking only works once enough same-stage peers exist. Fallback: use absolute bounty velocity in early months |
| 7 | **ERP project-member allocation API** | Medium | STROMA reads project allocations for capacity model. ERP must expose `GET /api/project-members?department_id=...` |
| 8 | **Stage gate Slack reply parsing** | Medium | If dept head replies to gate flag via Slack, STROMA must parse and record. Build same pattern as CELL PM approval reply parser |
| 9 | **pgvector not needed for v1** | Low | Unlike IRIS/CELL/CORTEX, STROMA v1 has no semantic retrieval requirement. Do not install pgvector on stroma_db unless chatbot retrieval is added later |
| 10 | **`@app.on_event` deprecated** | Low | Use `lifespan` context manager from day one — do not follow CELL's pattern here |

---

## 17. Key Design Decisions & Rationale

| Decision | Rationale |
|---|---|
| Monthly cadence, not real-time | People data moves slowly. Interns churn fast — over-indexing on daily signals creates noise. Monthly snapshots are cheaper, more stable, and more actionable. |
| Growth score is deterministic, not LLM | Same philosophy as CORTEX health score. Dept heads act on this number — it must be auditable and explainable. LLM reserved for chatbot and free-text notes only. |
| Bounty as universal work-tracking currency | Bounties mean real money only for intern_bounty stage. For all other stages they are a productivity signal. STROMA always interprets bounty data in context of stage. |
| Leech flag is a recommendation, not an action | STROMA never terminates anyone. It surfaces the signal with a recommendation. Human decision always required. |
| Stage gate decisions recorded explicitly | Every progression, extension, or let-go must be logged in `stage_transitions`. This is the audit trail for HR and for STROMA's own trajectory computation. |
| Skill profiles written back to HRMS | STROMA should not become a second source of truth for skills. The intranet HRMS is the canonical record. STROMA enriches it, not replaces it. |
| No pgvector in v1 | STROMA's intelligence is tabular and time-series based. Semantic search is not needed until the chatbot requires past-conversation retrieval. Add later if needed. |
| CELL is the source of task performance truth | STROMA never reads the CELL database directly. Clean API boundary — CELL owns task execution data, STROMA consumes summaries. |
| Separate `stroma_db` from `cell_db` and `cortex_db` | Each agent owns its data. Shared schema across agents creates coupling that breaks independently deployable services. |

---

*STROMA — IRIS perceives. CELL executes. CORTEX thinks. STROMA sustains.*
