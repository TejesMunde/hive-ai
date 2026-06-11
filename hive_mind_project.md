# Hive Mind — Project Documentation

> A persistent memory and continuity system for AI coding agents.
> Built so any agent, on any project, picks up exactly where the last one left off.

---

## What Is Hive Mind?

Every time you switch AI coding agents — Claude Code, Cursor, Windsurf — you lose context. You re-explain the stack. You re-explain why you chose PostgreSQL over MongoDB. The agent suggests an approach you already tried and rejected two weeks ago. You start from zero every single time.

Hive Mind fixes this.

It is a **project nervous system** — a background layer that silently captures every decision, every dead end, every open task, and every architectural choice made during development. When a new agent opens the project, it doesn't read a summary. It gets the exact slice of memory relevant to its current task, including what failed, who decided it, and what's still unresolved.

The goal is simple: **make AI development feel continuous regardless of which agent you use.**

---

## The Core Problem It Solves

| Without Hive Mind | With Hive Mind |
|---|---|
| Re-explain stack every new session | Agent reads project memory automatically |
| Agent suggests already-rejected approaches | Dead ends are stored and surfaced |
| Context lost when switching agents | Structured handoff packet transferred |
| No record of why decisions were made | Full decision provenance stored |
| Manual memory management | Zero manual input after setup |

---

## Architecture Overview

```
Developer codes
      ↓
Git commits + file saves
      ↓
┌─────────────────────────────────┐
│           HIVE MIND             │
│                                 │
│  Write Guard → Memory Store     │
│       ↓              ↓          │
│  Staging Area   Hot/Warm/Cold   │
│                 Tier System     │
└─────────────────────────────────┘
      ↓
Agent reads relevant context slice
(~500 tokens hot + ~2500 tokens warm)
      ↓
Agent works with full project awareness
```

### Storage Tiers

| Tier | Contents | When Agent Sees It | Token Limit |
|---|---|---|---|
| Hot | Current open tasks + latest snapshot | Every call | ~500 tokens |
| Warm | Decisions + architecture choices | On demand | ~2,500 tokens |
| Cold | Full history, dead ends, old logs | Explicit retrieval only | Unlimited |

### Database Schema (SQLite → PostgreSQL)

```
decisions     — what was decided, why, by which agent, confidence score
snapshots     — file structure, active stack, current module at a point in time
open_tasks    — unresolved work items, assigned agent, status
staging       — flagged records pending human review
```

---

## Deployment Paths

Hive Mind supports three deployment modes. The user chooses once at `hive init` and the system configures itself — no reconfiguration needed.

### Local (Recommended to start)
- Runs on the developer's machine
- SQLite database, zero setup, zero cost
- Install once: `pip install hive-mind && hive init`
- Best for: solo developers, privacy-sensitive projects

### Team (Self-hosted)
- REST API server shared across the team
- PostgreSQL backend, Docker deployment
- Every agent on every machine reads the same memory
- Best for: teams where multiple developers share a codebase

### Cloud (Hosted SaaS)
- Hive Mind hosted at `api.hive-mind.dev`
- Access via API key per project
- Dashboard for reviewing staged records
- Best for: commercial offering to other developers

### Migration
Switching paths is always one command:
```bash
hive migrate --from local --to team --server http://your-server:8000
```

---

## How It Runs — Zero Manual Input After Setup

```bash
# One time, ever
pip install hive-mind
hive init
```

After that:

- **Git hook** installed automatically — every commit updates Hive memory silently
- **File watcher** runs in background — every save updates the project snapshot
- **Agent config** written globally — `~/.claude/CLAUDE.md`, `~/.cursor/rules`, `~/.windsurf/rules` all updated so every agent uses Hive without being told
- **New projects** auto-bootstrapped — agent detects missing `hive.config.json` and runs `hive bootstrap` silently in under one second

The only manual interaction is a daily staging review:
```bash
python -m hive.cli.staging list
python -m hive.cli.staging accept <id>
python -m hive.cli.staging reject <id>
```

---

## The Write Guard

Every write to memory passes through a validator before it touches the database. This is the most critical component — corrupt memory is worse than no memory.

```
New information → Extract → Validate → Deduplicate → Commit
                                ↓
                          (if invalid)
                                ↓
                          Staging table
                          (human reviews daily)
```

### Validation Rules (as of Day 2)

| Rule | What It Catches | Action |
|---|---|---|
| Missing required fields | Empty `what`, `description`, `file_structure` | Staged |
| Too vague | Under 5 words in key field | Staged |
| Missing `why` on decisions | Decisions without reasoning | Staged |
| Exact duplicate | Identical record already exists | Staged |
| Fuzzy duplicate | >45% Jaccard token overlap with existing record | Staged |
| Contradiction | Same subject, opposite choice | Staged |

Staged records are never deleted — they wait for human review. This is how you learn what the system is getting wrong before you automate it.

---

## Project Roadmap

### Phase 1 — Core Memory Layer ✅ In Progress (Day 2 of 7 complete)
Foundation. Storage, write guard, hot/warm/cold tiers, staging review CLI.

### Phase 2 — Semantic Retrieval Engine
Replace keyword search with embedding-based cosine similarity. Agents get 5 relevant facts, not 500 irrelevant ones.

### Phase 3 — Decision Provenance + Dead Ends
Store what was rejected and why. Link every decision to what it replaced. Prevent agents from re-suggesting already-discarded approaches.

### Phase 4 — Confidence-Weighted Memory
Every record tagged with a confidence score (0.0–1.0). Scores decay over time, strengthen when agent output confirms the decision. Records below 0.3 auto-archive to cold storage.

### Phase 5 — Agent Handoff Protocol
Structured handoff packet: what was in progress, open questions, next intended action. Auto-routing based on agent expertise history.

### Phase 6 — Auto-Learning from Project History
Hive reads Git commits, file changes, and architecture shifts automatically. Zero manual writes required in normal usage.

---

## Progress Log

### Day 1 — Foundation ✅

**Goal:** SQLite running, three tables created, basic read/write proven end-to-end.

**Built:**
- `hive/db/setup.py` — SQLite initialisation, four tables (`decisions`, `snapshots`, `open_tasks`, `staging`), indexes
- `hive/core/guard.py` — Write guard v1: required fields, vague entry detection, exact duplicate check, contradiction check
- `hive/core/writer.py` — `write_memory()`: the single entry point for all writes, routes to guard then commits
- `hive/core/reader.py` — `read_memory()`: hot + warm tier retrieval with keyword ranking
- `hive/__init__.py` — Public API surface: `init_db`, `write_memory`, `read_memory`
- `tests/test_day1.py` — End-to-end proof: 3 decisions + 1 snapshot + 2 tasks committed, 3 bad records caught, query returns correct context

**Key outcome:** An agent can ask a question about the project and get a correct, token-budgeted answer from memory.

---

### Day 2 — Battle-Tested Write Guard + Staging CLI ✅

**Goal:** Write guard catches every class of bad data. Staging review CLI works end-to-end.

**Built:**
- `hive/core/guard.py` — Write guard v2:
  - Fuzzy duplicate detection (Jaccard token overlap, threshold 0.45)
  - Missing `why` field rule — decisions without reasoning are staged
  - Upgraded contradiction detection
- `hive/core/writer.py` — Three new functions:
  - `close_task()` — marks task done, idempotent
  - `promote_from_staging()` — human accepts flagged record, commits with `human-reviewed` tag
  - `reject_from_staging()` — permanently removes bad record
- `hive/cli/staging.py` — Full staging review CLI with colour output:
  - `list` — shows all staged records with preview and reason
  - `accept <id-prefix>` — promotes to memory
  - `reject <id-prefix>` — removes permanently
  - `clear --project` — bulk reject for a project
- `tests/test_day2.py` — 6 guard rules tested, staging accept/reject flow proven, `close_task()` idempotency verified

**Key outcome:** The write guard now catches vague entries, exact duplicates, fuzzy rewordings, missing reasoning, and contradictions. Nothing bad reaches memory without human sign-off.

---

### Day 3 — Planned
`read_memory()` accuracy tightened. Keyword ranking improved. Real accuracy benchmark against actual project data.

### Day 4 — Planned
Feed 10 real decisions from real project. Run 5 real agent queries. Measure retrieval accuracy. Fix everything that breaks.

### Day 5 — Planned
Staging review integrated into daily workflow. Auto-reject threshold tuned from staging data.

### Week 2 — Planned
Run Hive alongside real agent work for 7 days. Log every failure. Phase 1 milestone: agent reads from memory and gets correct answer without touching the codebase.

---

## File Structure

```
hive_mind/
├── hive/
│   ├── __init__.py              ← Public API: init_db, write_memory, read_memory
│   ├── db/
│   │   ├── __init__.py
│   │   └── setup.py             ← SQLite init, tables, indexes
│   ├── core/
│   │   ├── __init__.py
│   │   ├── guard.py             ← Write guard: validate before every write
│   │   ├── writer.py            ← write_memory, close_task, staging promotion
│   │   └── reader.py            ← read_memory: hot/warm tier retrieval
│   └── cli/
│       ├── __init__.py
│       └── staging.py           ← Staging review CLI
└── tests/
    ├── test_day1.py             ← Day 1 end-to-end tests
    └── test_day2.py             ← Day 2 guard + staging tests
```

---

## How to Run

```bash
# Install dependencies (none required — pure stdlib for now)
cd hive_mind

# Run Day 1 tests
python tests/test_day1.py

# Run Day 2 tests
python tests/test_day2.py

# Review staging items
python -m hive.cli.staging list
python -m hive.cli.staging accept <first-8-chars-of-id>
python -m hive.cli.staging reject <first-8-chars-of-id>
```

---

## Technical Decisions Made

| Decision | Why |
|---|---|
| SQLite over PostgreSQL (for now) | Zero setup, inspectable, migratable later. Wrong tool for Phase 6, right tool for Phase 1. |
| Jaccard token overlap over SequenceMatcher | SequenceMatcher scores character similarity. Jaccard scores concept overlap. "We are using FastAPI instead of Flask" is 65% SequenceMatcher match, 50% Jaccard — Jaccard catches the semantic duplicate correctly. |
| Staging over rejection | Deleted bad data gives no signal. Staged bad data teaches you what the system is getting wrong. Every staged record is feedback. |
| Flat file index from day one | Phase 2 requires an index for semantic retrieval. Building it now (even as keyword matching) means swapping in embeddings without rewriting the retrieval layer. |
| No external dependencies in Phase 1 | Pure stdlib means zero install friction. Embeddings (Phase 2) and FastAPI (Team mode) are deferred until they're actually needed. |
| Write guard before every commit | One corrupt record poisons every future agent call that queries it. The guard is non-negotiable, not optional infrastructure. |

---

## What Makes This Different from a RAG System

A standard RAG system retrieves documents based on query similarity. Hive Mind is not that.

| RAG | Hive Mind |
|---|---|
| Retrieves documents | Retrieves structured decisions |
| No write validation | Write guard on every record |
| Static knowledge base | Continuously updated from Git + agents |
| No provenance | Full decision lineage — what replaced what |
| No dead ends | Rejected approaches stored and surfaced |
| No handoff | Structured agent handoff packet |
| Query → answer | Task → context slice → agent works |

Hive Mind is opinionated about *what* gets stored (decisions, not documents), *how* it gets stored (validated, structured, confidence-scored), and *what agents get back* (a task-specific context slice, not a similarity-ranked document list).

---

*Documentation current as of Day 2, Phase 1.*
*Last updated: June 2026*
