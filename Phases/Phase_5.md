# Phase 5 — Agent Handoff Packets & Expertise Routing

**Status:** ✅ IMPLEMENTED on branch `phase-5-handoff-routing` (test_day10 green;
days 1–9 + retrieval benchmark unchanged — hybrid 79.2/91.7/0.856).
**Goal:** make Hive *multi-agent*. Two capabilities:
1. **Handoff packets** — when one agent stops and another picks up, produce a
   compact, persisted packet: current state + *what changed since the last
   handoff*. The delta is the continuity payload — it answers "what happened
   while I was away" without re-reading the whole corpus.
2. **Expertise routing** — given a task, rank the agents by how relevant their
   prior decisions are, so work goes to whoever has the most applicable history.

Decisions locked with the user before coding (see §5).

---

## 1. Handoff packets

A handoff is a thing you *create and hand over*, so it is **persisted** — that is
what lets the next packet compute "since the last handoff".

### Schema — new table (no migration; brand-new table)

```sql
CREATE TABLE IF NOT EXISTS handoffs (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    from_agent  TEXT,
    to_agent    TEXT,
    payload     TEXT NOT NULL,   -- JSON: the full packet (state + delta)
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_handoffs_project ON handoffs(project, created_at);
```

Brand-new table → goes in the main `executescript`; nothing to ALTER.

### API — `core/handoff.py`

```python
create_handoff(project, from_agent=None, to_agent=None) -> dict
```

Builds, persists, and returns the packet:

```
{
  "id": ...,
  "project": ...,
  "from_agent": ..., "to_agent": ...,
  "created_at": ...,
  "state": {
     "open_tasks":      [...],          # live work items
     "latest_snapshot": {...} | None,   # current structure
     "top_decisions":   [...],          # most relevant live decisions (decayed)
  },
  "delta": {                            # since the previous handoff for this project
     "since": <prev handoff created_at> | None,   # None on the first ever handoff
     "decisions_added":  [...],
     "dead_ends_added":  [...],
     "tasks_closed":     [...],         # tasks closed since `since`
     "tasks_opened":     [...],
  }
}
```

- **Delta boundary** = the `created_at` of the most recent *prior* handoff for the
  project (looked up before inserting this one). First handoff → `since=None` and
  the delta lists everything (full history) so a cold-start agent still gets it.
- The delta is computed from `created_at` timestamps on `decisions`, `dead_ends`,
  and `open_tasks` (+ a `closed_at` — see note). Archived/superseded decisions are
  included in the delta (they're "what changed"), but `top_decisions` in `state`
  uses the normal warm-tier rules (archived excluded).
- `get_handoff(handoff_id)` and `latest_handoff(project)` read packets back.
- Emits an audit event `handoff_create`.

> **Note — task close timestamps.** `open_tasks` currently flips `status='done'`
> with no timestamp, so "tasks closed since X" isn't directly queryable. Phase 5
> adds a nullable `closed_at` column (idempotent migration) set by `close_task`.
> Without it the delta can only report *currently* open vs not — `closed_at` makes
> "closed during this interval" precise.

### Why persisted + delta (locked decision)

On-demand state alone is just a second flavour of `read_memory`. The persisted
delta is the actual product: it turns "here's the memory" into "here's what moved
since you last looked", which is the whole point of a handoff between agents.

---

## 2. Expertise routing

```python
route_task(project, task, top_n=3) -> list[dict]
```

Advisory ranking — **never mutates**, never auto-assigns (locked decision).
Returns agents best suited to a task, with evidence:

```
[
  { "agent": "claude-code",
    "score": 0.83,
    "evidence": [ {decision_id, what, relevance}, ... ]  # top matches
  },
  ...
]
```

### Signal — retrieval-relevance, decay-aware (locked decision)

Reuse the existing retrieval stack rather than invent a new one:

```
for each live decision d authored by agent a:
    rel(task, d) = IDF-overlap(task, d)            # reader._idf_score
                   (+ dense cosine when HIVE_DENSE on, via fuse machinery)
    weight       = effective_confidence(d)         # Phase 4 decay
    contribution = rel * weight
agent_score(a)   = sum of contributions over a's decisions
rank agents by score desc; attach top-k decisions as evidence
```

- **Decay-aware**: a stale decision contributes less, so an agent whose relevant
  expertise is old ranks below one with fresh, reinforced work in the area. This
  is exactly why Phase 4 came first.
- **Dense optional**: when `fastembed`/`HIVE_DENSE` is on, blend dense relevance
  the same way the reader does; otherwise IDF-overlap alone. No hard dependency.
- Agents with zero relevant decisions are omitted (not ranked at 0).
- Pure read — no writes, so it can never corrupt state.

---

## 3. Public API additions

```python
from hive import create_handoff, latest_handoff, get_handoff, route_task
```

(`reinforce_decision` etc. from Phase 4 stay.)

---

## 4. Acceptance criteria

- [x] `handoffs` table created idempotently; `open_tasks.closed_at` added via migration
- [x] `create_handoff` persists a packet and returns state + delta
- [x] Delta is bounded by the previous handoff: a second handoff with no activity
      in between has empty `decisions_added`/`dead_ends_added`/`tasks_closed`
- [x] First-ever handoff has `since=None` and reports full history
- [x] `tasks_closed` reflects tasks closed *within the interval* (needs `closed_at`)
- [x] `state.top_decisions` excludes archived; delta may include archived/superseded
- [x] `get_handoff` / `latest_handoff` round-trip
- [x] `route_task` ranks the agent with the most relevant (decayed) authorship top,
      returns evidence, and never mutates any row
- [x] A stale-but-on-topic agent ranks below a fresh-on-topic agent (decay applied)
- [x] `route_task` returns [] cleanly when no agent has relevant decisions
- [x] `test_day10.py` green; days 1–9 + `bench_recall` (79.2/91.7/0.856) unchanged

### Note from the build
The decay-routing test initially used two near-identical Kafka decisions — the
write guard (correctly, no bypass) flagged them as fuzzy duplicates so the second
never committed. Fixed by making the two decisions genuinely distinct while both
still matching the query. Good reminder that the guard applies in tests too.

---

## 5. Decisions locked with the user

1. **Handoff model** → persisted `handoffs` table + delta-since-last-handoff.
2. **Routing signal** → retrieval-relevance weighted by Phase 4 effective confidence.
3. **Routing output** → advisory ranking with evidence; never auto-assign.

---

## 6. Open questions (resolve during build)

- `top_decisions` count in the packet: start at 5, tune later.
- Routing across very large agent histories is O(decisions); fine at current
  scale (same loop as the reader). Vectorize alongside the TF-IDF bench fix if it
  ever matters.
- Cross-project routing/handoff is out of scope — everything is per-project, as
  with the rest of Hive.
