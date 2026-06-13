# Phase 4 — Confidence Decay, Cold Archive, Contradiction Detection v2

**Status:** ✅ IMPLEMENTED on branch `phase-4-decay-archive` (test_day9 green;
days 1–8 + retrieval benchmark unchanged — hybrid 79.2/91.7/0.856).
**Goal:** make memory *age* — old unreinforced decisions lose ranking weight,
replaced/stale ones move out of the warm tier into a queryable cold archive, and
the guard catches contradictions semantically, not just by literal noun-swap.

Decisions locked with the user before coding (see §6).

---

## 1. Confidence decay — lazy exponential half-life

Confidence is already a stored field (`decisions.confidence REAL DEFAULT 1.0`)
with a tiny ranking nudge the reader comments as "decay-ready". Phase 4 makes it
actually decay.

**Model: lazy exponential half-life — computed at read time, never materialized.**

```
age_days     = (now - created_at) / 1 day
eff_conf     = stored_confidence * 0.5 ** (age_days / HALF_LIFE_DAYS)
```

- `HALF_LIFE_DAYS = 90` (a decision unreinforced for 3 months counts half).
- The stored `confidence` column is **never mutated** by decay — decay is a pure
  function of `(stored_confidence, age)` evaluated per query. No background job,
  no migration churn, deterministic and replayable. Fits a local-first tool.
- `created_at` doubles as the "last reinforced at" clock.

**Reinforcement** — when the same decision is re-affirmed, bump stored confidence
and reset the clock so decay restarts from now:

```python
reinforce_decision(decision_id, by=0.25)
#   confidence = clamp(confidence + by, 0.0, 1.0)   # ceiling is 1.0, not 2.0
#   created_at = now                                # resets the half-life clock
```

**Why the ceiling is 1.0, not 2.0 (design review fix).** An early cut capped
reinforcement at 2.0. But with a fixed `ARCHIVE_FLOOR = 0.25`, a stored value of
2.0 only crosses the floor at ~270 days vs ~180 for 1.0 — i.e. heavy
reinforcement *buys immunity from decay*. That is exactly backwards once Phase 6
auto-reinforces on related commits: the most-touched decisions (often the ones in
flux, about to change) would pin at 2.0 and stop decaying for the better part of a
year even after the project moved on. Capping at 1.0 makes confidence a pure
freshness/trust signal in `[0,1]`: reinforcement resets the *clock* (so an active
decision stays warm while touched) but never accumulates a reserve, so
post-abandonment warmth is always bounded to the base 180-day schedule. The
"reinforced ranks higher" effect is preserved through the recency boost (reset
`created_at`), not through an above-baseline confidence. Confidence is also
clamped to `[0,1]` on every **write**, so the invariant holds end-to-end.

**Reader integration** — replace the static `conf_adj = (conf-1.0)*0.05` with the
decayed value:

```
eff_conf = conf * 0.5 ** (age_days / HALF_LIFE_DAYS)
conf_adj = (eff_conf - 1.0) * CONFIDENCE_WEIGHT      # same weight, decayed input
```

The nudge stays small (`CONFIDENCE_WEIGHT = 0.05`) so decay re-orders ties and
near-ties without overturning keyword/dense relevance — the benchmark must not
move. A fresh decision (age 0) gives `eff_conf == stored`, so existing behaviour
is preserved on day 0.

---

## 2. Cold archive

A decision leaves the warm tier when it is no longer live. Three triggers
(all selected):

| Trigger | Rule |
|---------|------|
| **Superseded** | another decision's `supersedes_id` points at it → auto-archive |
| **Below confidence floor** | `eff_conf < ARCHIVE_FLOOR` (0.25) at evaluation time → auto-archive |
| **Explicit** | `archive_decision(id)` called directly |

**Schema** — one nullable timestamp column, added idempotently:

```sql
ALTER TABLE decisions ADD COLUMN archived_at TEXT;   -- NULL = live
CREATE INDEX IF NOT EXISTS idx_decisions_archived ON decisions(archived_at);
```

`archived_at IS NULL` = live (warm tier). Non-null = cold. We store the timestamp
(not a bool) so the archive itself has history.

**When triggers fire:**
- *Superseded*: in `write_memory`, right after a decision with `supersedes_id`
  commits, set `archived_at = now` on the superseded row (it's been replaced).
- *Below floor*: evaluated lazily. A `sweep_archive(project)` helper (explicit /
  cron-able) walks live decisions, computes `eff_conf`, and archives those under
  the floor. NOT run inside `read_memory` (keeps reads side-effect free and the
  benchmark honest).
- *Explicit*: `archive_decision(id)` / `unarchive_decision(id)`.

**Retrieval behaviour — excluded by default, opt-in flag:**

```python
read_memory(project, query)                          # warm tier excludes archived
read_memory(project, query, include_archived=True)   # includes them
```

`get_provenance(decision_id)` still resolves archived rows (history must remain
queryable — that is the whole point of an archive vs a delete).

---

## 3. Contradiction detection v2

v1 (`guard._find_contradiction`) only fires on the narrow pattern "same nouns
around an opposition marker (`over`/`vs`/`instead of`/`not`/`rather than`) with
sides swapped". It misses semantically-opposed decisions worded differently
("Adopted REST for the public API" vs "Moved the public API to gRPC").

**v2: add a dense-similarity path, keep v1 as the precise fallback.**

```
1. v1 swapped-noun check  → if it fires, flag (precise, keep as-is).
2. else, if dense available:
     sim = cosine(embed(new_what), embed(existing_what))
     if sim >= CONTRA_SIM (0.80) AND share a decision subject
        AND an opposition/replacement signal is present
        → flag as a likely contradiction (route to staging, never auto-drop).
```

Guards against false positives:
- High threshold (`CONTRA_SIM = 0.80`) — two decisions about the *same* subject.
- Must ALSO carry an opposition/replacement cue (a marker word, or one is a
  `supersedes_id` candidate) — pure topical similarity is NOT a contradiction
  (two complementary auth decisions must not collide).
- A flagged contradiction goes to **staging** (human review), never auto-reject —
  same as v1. False positives cost a review click, not lost data.
- Dense path is optional: when `fastembed`/`HIVE_DENSE` is off, v2 silently
  degrades to v1. No hard dependency.

**Validation requirement:** the v2 path must NOT raise the false-positive rate on
the benchmark corpus (it has near-duplicate, non-contradictory decisions). Add a
contradiction precision check to the Phase 4 test before merging.

---

## 4. Constants (one place — `core/policy.py` or a new `core/decay.py`)

```python
HALF_LIFE_DAYS    = 90      # confidence halves every 90 unreinforced days
CONF_CAP          = 1.0     # confidence ceiling — capped at 1.0 (see §1 note), so
                            # reinforcement resets the clock but buys no decay immunity
ARCHIVE_FLOOR     = 0.25    # eff_conf below this → eligible for cold archive
CONTRA_SIM        = 0.80    # dense cosine threshold for contradiction v2
REINFORCE_STEP    = 0.25    # default reinforcement bump
```

---

## 5. Acceptance criteria

- [x] `decisions.archived_at` added idempotently (index built in `_migrate` AFTER the ALTER)
- [x] Decay is a pure read-time function; stored confidence unchanged by reads
- [x] `reinforce_decision()` bumps confidence (capped at 1.0) and resets the clock
- [x] Day-0 behaviour unchanged (eff_conf == stored at age 0) → benchmark unmoved
- [x] Superseding a decision auto-archives the superseded row
- [x] `sweep_archive()` archives decisions under the confidence floor; reads do not
- [x] `read_memory` excludes archived by default; `include_archived=True` surfaces them
- [x] `get_provenance` still resolves archived decisions
- [x] Contradiction v2 flags a semantically-opposed reword v1 misses, routes to staging
- [x] v2 adds zero false positives on the eval corpus; degrades to v1 without dense
- [x] `test_day9.py` green; days 1–8 + `bench_recall` (79.2/91.7/0.856) unchanged

### Bugs caught during the build (by the regression gate)
1. **`sweep_archive` write-lock deadlock** — it called `audit_log()` (a separate
   connection) mid-transaction, which silently rolled back the archive while
   still reporting success. Fixed: commit first, audit after.
2. **Migration index ordering** — `CREATE INDEX … ON decisions(archived_at)` sat
   in the main `executescript`, which runs before `_migrate()`. On a pre-Phase-4
   DB the `CREATE TABLE IF NOT EXISTS` is a no-op, so the index referenced a
   column that didn't exist yet → days 1–8 all failed to `init_db`. Fixed by
   moving the index into `_migrate` after the `ALTER`.
3. **Stale Phase 3 test assumption** — test_day8 superseded a decision then
   expected it in the default warm tier; Phase 4 auto-archives it, so the
   assertion was updated to `include_archived=True`.

---

## 6. Decisions locked with the user

1. **Decay model** → lazy exponential half-life (no materialized decay pass).
2. **Archive triggers** → all three: superseded, below confidence floor, explicit.
3. **Archived in retrieval** → excluded by default, opt-in `include_archived` flag.

---

## 7. Open questions (resolve during build)

- Half-life of 90 days is a guess — expose it as a constant, tune later against
  real usage. No data yet to fit it.
- `sweep_archive` cadence: explicit call for v1. A scheduler/hook is Phase 6.
- Contradiction v2 "shared subject" detection: start with noun-overlap ≥ 1
  significant token; revisit if it proves too loose.
