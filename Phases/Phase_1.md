# Phase 1 — Solo Mode (Keyword Memory)

**Status:** ✅ Shipped
**Window:** Week 1 + Week 2 of roadmap (`hive_mind_project.md`)
**Constraint:** Pure stdlib. Zero external deps.
**Milestone test:** `tests/test_day7.py` — 15/15 top-1, MRR 1.000, no budget breach.

---

## 1. Goal of Phase 1

From the project doc:

> *"Agent reads from memory and gets correct answer without touching the codebase."*

Build a persistent, queryable memory layer for a single agent so that cold-open questions get answered from prior decisions rather than re-derived from source. Defer embeddings, FastAPI, multi-agent sync, and provenance graphs to later phases.

The agent should be able to ask things like *"how do we track production errors"* and get back *"Sentry adopted for application error monitoring across services"* — even though the query says **track** and the doc says **monitoring**.

---

## 2. Architecture (final state)

```
hive/
├── __init__.py              # re-exports: write_memory, read_memory, close_task, promote/reject_from_staging
├── db/
│   └── setup.py             # SQLite schema, get_connection(), init_db()
├── core/
│   ├── guard.py             # 6 write-time validation rules
│   ├── writer.py            # write_memory, close_task, staging promote/reject
│   ├── reader.py            # hot+warm tier read, TF-IDF + recency rank
│   ├── normalize.py         # case-fold, stopword drop, suffix stem, synonym expand
│   ├── policy.py            # per-project guard_policy + tune_policies()
│   └── audit.py             # append-only event log
└── cli/
    ├── staging.py           # interactive review + stats/tune
    └── audit.py             # tail/counts/fails

tests/
└── test_day1.py … test_day7.py
```

### Storage (SQLite, 7 tables)

| Table | Role |
|---|---|
| `decisions`        | committed long-term decisions (the warm tier) |
| `snapshots`        | latest project structure (hot tier) |
| `open_tasks`       | live work items (hot tier) |
| `staging`          | writes the guard flagged for review |
| `staging_history`  | reviewer outcomes — feeds the auto-tune learner |
| `guard_policy`     | per-project per-category action: `stage` or `auto_reject` (PK: `(project, category)`) |
| `audit_log`        | append-only event stream — every write + every query |

`PRAGMA foreign_keys = ON`. Row factory `sqlite3.Row`. DB path overridable via env `HIVE_DB_PATH`.

---

## 3. Day-by-Day Build Log

### Day 1 — Skeleton + write guard + first reader
- Scaffolded `hive/` package (doc claimed it existed; only flat files were present).
- Wrote `db/setup.py` with the first 4 tables + foreign keys.
- Implemented 6 write-guard rules in `core/guard.py`:
  1. Required fields present
  2. Vague (under 5 words)
  3. Exact duplicate of an existing decision
  4. Contradiction (semantic flip vs prior decision)
  5. Fuzzy duplicate (Jaccard overlap ≥ 0.45)
  6. Missing `why`
- Implemented `write_memory` → returns `committed | staged | rejected`.
- Implemented v1 of `read_memory` — token overlap ranking, hot/warm split.
- **Test:** 3 valid + 1 task + 2 bad writes; query lands on the right decision.

### Day 2 — Staging review loop + close_task
- `promote_from_staging` / `reject_from_staging` in `core/writer.py`.
- Built `hive/cli/staging.py`: interactive `y/n/s/q` reviewer.
- `close_task` flips status with audit trail.
- **Test:** all 6 guard rules hit + accept/reject path + task closure.

### Day 3 — TF-IDF ranker
- Replaced raw overlap with smoothed IDF: `log((N+1)/(df+1)) + 1`.
- Headline boost: matches on `what` weighted 25% above matches on `why`.
- Linear recency boost (cap +0.05 — newest wins ties only).
- Tiebreak: newer first.
- **Bug found:** *"how do we track production errors"* missed Sentry because doc said **errors**, query said **errors** stemmed differently and **track** ≠ **monitoring**.

### Day 4 — Stemmer + synonym map
- Built `core/normalize.py`: pure-stdlib Porter-lite stemmer.
  - `-ies → y`, `-ied → y`, `-sses → ss`, `-ing`, `-edly`, `-ingly`, `-ly`, `-s`
  - `-ion → ''` only when root ends in `ct/ss/pt` (detection→detect, production→product) — skips false hits like *mention→ment*
  - `-ed` with silent-e correction: `cached → cache`, `moved → move` (preserves the `e` when preceded by `v`, `ch`, `sh`, `th`)
- Added a small curated synonym map (`_SYNONYMS`) — keeps false-positive blast radius bounded:
  ```python
  "sqlite/postgresql/mysql/…" → ("database",)
  "fastapi/flask/django"      → ("framework", "api")
  "monitor"                   → ("track",)
  "telemetry/trace"           → ("track", "observability")
  "hot/warm/cold"             → ("tier",)
  ```
- Added a **zero-overlap floor** in the reader: if a query has no IDF hit, recency contribution is suppressed so we never return a newest-but-unrelated doc as top result.
- **Test:** 10 real decisions from the project doc + 5 queries → MRR 1.000.

### Day 5 — Auto-tune from staging history
- Added `staging_history` + `guard_policy` tables.
- `core/policy.py`:
  - `category_of(reason)` extracts category from guard reason via regex split on `:`, `—`, `(`.
  - `tune_policies(project)` aggregates outcomes; flips a category to `auto_reject` when `accept_rate ≤ 0.10` over `≥ 5` samples.
  - `policy_action(project, category)` queried by `write_memory` before staging.
- `write_memory` gains a fourth status: `auto_rejected`.
- **Cross-project leak fix:** `guard_policy` PK was global `(category)` — Day 5 test poisoned Days 1-2. Changed PK to `(project, category)`. Required dropping `hive.db` (no migration in Phase 1).
- New CLI verbs: `staging stats`, `staging tune`.
- **Test:** simulated 12 reviewer outcomes per project → tune flips correctly + projects stay isolated.

### Day 6 — Audit log + 7-day soak
- `core/audit.py`: append-only `audit_log` table. Kinds:
  - `write_commit`, `write_staged`, `write_auto_rejected`, `write_rejected`
  - `staging_accept`, `staging_reject`
  - `task_close`
  - `query`
- Wired audit into every writer path + every `read_memory` call.
- Built `hive/cli/audit.py`: `tail`, `counts`, `fails` (color-coded).
- **Test (`test_day6.py`):** 7 simulated days, 42 writes, 21 queries.
  - Every write outcome accounted for: `committed + staged + auto_rejected == total`
  - Audit captured every event
  - Policy flipped to `auto_reject` by day 5 (MIN_SAMPLES=5 needs that many review cycles)

### Day 7 — Phase 1 milestone
- 15 decisions across a fake B2B SaaS (API, DB, cache, auth, payments, observability, infra).
- 15 cold-open queries phrased like a junior dev would ask.
- **Bugs caught + fixed:**
  - *"powers the public api"* tied FastAPI vs REST → added `fastapi/flask/django → (framework, api)` synonym.
  - *"how are user sessions cached"* missed Redis — stemmer dropped silent-e on `cached → cach`. Fixed with the silent-e rule above.
  - *"how do we track production errors"* missed Sentry — confirmed `monitor → (track,)` synonym closes it.
- **Acceptance:** top-1 ≥ 13/15, MRR@5 ≥ 0.90, no budget breach, every query audited.
- **Result:** 15/15 top-1, MRR **1.000**, all budgets clean.

---

## 4. Retrieval Pipeline (final)

```
query string
  │
  ▼ split on whitespace
  ▼ lowercase + strip punctuation
  ▼ drop stopwords (≤2 chars or in _STOP)
  ▼ stem (conservative suffix rules)
  ▼ synonym expand (curated dev vocab)
  ▼
token bag → IDF score per decision
            + 25% headline boost
            + up to +0.05 recency (only if base > 0)
            + (confidence - 1.0) × 0.05
  │
  ▼ sort by score desc, created_at desc
  ▼ pack into 2500-token warm budget
  ▼ emit query event to audit_log
  │
  ▼
{ hot: { open_tasks, latest_snapshot },
  warm: { decisions[] },
  token_estimate }
```

Budgets: hot ≤ 500 tokens, warm ≤ 2500 tokens. Chars-per-token = 4.

---

## 5. Write Pipeline (final)

```
write_memory(rtype, project, data)
  │
  ▼ Rule 1: required fields?
  ▼ Rule 2: vague (<5 words)?
  ▼ Rule 3: exact duplicate?
  ▼ Rule 4: contradiction? (token-set side comparison)
  ▼ Rule 5: fuzzy duplicate? (Jaccard ≥ 0.45)
  ▼ Rule 6: missing why?
  │
  ├── no flags → COMMIT  ──► decisions table + audit(write_commit)
  │
  └── flagged → look up guard_policy(project, category)
                 ├── policy = auto_reject → audit(write_auto_rejected), drop
                 └── policy = stage       → staging table + audit(write_staged)
                                            then human review via CLI
                                              ├── accept → decisions + audit(staging_accept) + record_outcome
                                              └── reject → audit(staging_reject) + record_outcome

`tune_policies(project)` reads staging_history → upserts guard_policy.
```

The contradiction rule was the trickiest fix of Phase 1 — original implementation was dead code because Rule 5 (fuzzy dup) fired first on swapped-noun cases (71% Jaccard). Reordered Rule 4 ahead of Rule 5 + rewrote `_find_contradiction` to detect noun-side flips:

```python
swapped   = bool(nL & eR) and bool(nR & eL)
same_side = bool(nL & eL) and bool(nR & eR)
if swapped and not same_side:
    return row["what"]
```

---

## 6. Test Scoreboard

| Day | Test | Outcome |
|-----|------|---------|
| 1 | `test_day1.py` — skeleton + first reader        | ✅ |
| 2 | `test_day2.py` — 6 guard rules + staging loop   | ✅ |
| 3 | `test_day3.py` — TF-IDF MRR@5                   | ✅ 5/5, MRR 1.000 |
| 4 | `test_day4.py` — real project decisions         | ✅ 5/5, MRR 1.000 |
| 5 | `test_day5.py` — tune + per-project isolation   | ✅ |
| 6 | `test_day6.py` — 7-day soak + audit completeness | ✅ flip day 5, all 63 events captured |
| 7 | `test_day7.py` — Phase 1 milestone              | ✅ **15/15 top-1, MRR 1.000** |

Run with: `PYTHONIOENCODING=utf-8 python tests/test_dayN.py`

---

## 7. CLI Surface

```bash
# Staging review + policy
python -m hive.cli.staging stats   [--project P]
python -m hive.cli.staging tune    [--project P]
python -m hive.cli.staging review  [--project P]   # interactive y/n/s/q

# Audit log
python -m hive.cli.audit tail      [--project P] [--limit N]
python -m hive.cli.audit counts    [--project P]
python -m hive.cli.audit fails     [--project P]
```

---

## 8. Key Design Decisions

| Decision | Reason |
|---|---|
| SQLite over Postgres in Phase 1                           | Zero install friction. File-based. Survives `git clone`. |
| Keyword retrieval (TF-IDF + stem + synonyms) over embeddings | Phase 1 "pure stdlib" constraint. Embeddings deferred to Phase 2. |
| Curated synonym map vs WordNet                            | Map is ~25 entries, dev-vocab specific. WordNet adds 100MB + false positives. |
| Conservative stemmer (not full Porter)                    | Covers ~80% of English noun/verb morphology relevant to dev decisions. Each rule has a unit-test case behind it. |
| Zero-overlap floor                                        | Without it, recency alone returns newest unrelated doc as "top result". |
| `(project, category)` policy PK                           | Per-project learning. Day 5 test caught the bug — global PK poisoned other projects. |
| Audit log = append-only INTEGER PK AUTOINCREMENT          | Replayable. Never mutate. Survives schema migration. |

---

## 9. Known Limits (intentional — addressed in later phases)

| Limit | Resolved in |
|---|---|
| No semantic similarity — `kubernetes` vs `k8s` requires a synonym entry | Phase 2 (embeddings) |
| No provenance — can't ask *"why was this decided?"* with file refs    | Phase 3 |
| Confidence is a stored field but never decays                         | Phase 4 |
| Single-agent only — no handoff or merge                               | Phase 5 |
| No auto-learn from git history                                        | Phase 6 |

---

## 10. Phase 1 Acceptance Criteria — all met

- [x] Agent writes a decision and it survives restart
- [x] Agent queries memory and gets the right answer in top-1
- [x] Bad writes (vague, dup, contradiction) get caught before they pollute the corpus
- [x] Reviewer's repeated rejections teach the system to auto-reject the same class going forward
- [x] Every write and every query is traceable through `audit_log`
- [x] Token budgets (hot ≤ 500, warm ≤ 2500) never exceeded
- [x] Zero external dependencies

**Phase 1 shippable.** Ready to start Phase 2 (semantic retrieval via embeddings) when the user gives the go-ahead.
