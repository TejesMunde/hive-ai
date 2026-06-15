# 🐝 Hive Mind

**Persistent, cross-agent memory for AI coding agents.**

Every time you start a fresh AI coding session, the agent forgets everything. You
re-explain "we chose Postgres over Mongo because of multi-document transactions,"
re-describe the architecture, re-justify decisions that were settled weeks ago.
Hive Mind fixes that: it's a local, file-based memory store that any agent reads
before it works and writes to after — so decisions, dead ends, and context survive
across sessions **and across different agents**.

```python
from hive import read_memory, write_memory

# Before working: inherit everything the last agent knew
ctx = read_memory(project="my-api", query="add rate limiting to the public API")

# After deciding: leave it for the next agent
write_memory("decision", "my-api", {
    "what": "Token-bucket rate limiting at the gateway, 100 req/s per key",
    "why":  "Sliding-window was 3x the Redis ops; token-bucket is good enough",
})
```

---

## Why it's different

Most "memory" tools are a dump of embeddings you hope are relevant. Hive Mind is
opinionated about **quality** and **trust**:

- **A write guard nothing bypasses.** Every write passes 6 rules (required fields,
  vagueness, exact/fuzzy duplicates, contradictions, missing rationale) *before* it
  touches the store. Bad writes go to a review queue, not the bin — so even rejects
  are signal.
- **Hybrid retrieval that actually wins.** TF-IDF keyword precision fused with dense
  semantic recall (RRF). Beats pure keyword search on every metric and holds across
  54× corpus growth.
- **Memory that ages honestly.** Confidence decays on a half-life; stale decisions
  fall out of the working set; re-affirming one resets its clock. Computed at read
  time — stored data is never silently mutated.
- **Provenance, not just answers.** Record what you *ruled out* and link it to what
  you chose. Later: "what did we consider before this?"
- **It captures itself.** A git post-commit hook extracts decisions straight from
  commit messages — gated by a quality floor, never bypassing the guard.
- **Local-first, zero setup.** One SQLite file that survives `git clone`. No server,
  no vector DB, no cloud. Pure-stdlib core; the semantic layer is optional.

---

## Install

```bash
git clone https://github.com/TejesMunde/hive-mind.git
cd hive-mind

# Core is pure stdlib. For the semantic (dense) retrieval layer:
pip install numpy fastembed
```

The dense path is **optional** — if `fastembed` is absent or `HIVE_DENSE=0`, the
reader degrades silently to TF-IDF.

```python
from hive import init_db
init_db()   # idempotent: creates tables + runs migrations
```

---

## Quick start

```python
from hive import init_db, read_memory, write_memory
from hive.core.writer import close_task

init_db()
project = "my-api"

# Record a decision (passes the write guard)
write_memory("decision", project, {
    "what":  "Chose PostgreSQL for the primary OLTP store",
    "why":   "ACID guarantees and JSONB fit the workload better than Mongo",
    "agent": "claude-code",
})

# Track open work
write_memory("open_task", project, {"description": "Wire up connection pooling"})

# Later (or in another agent): retrieve ranked context
ctx = read_memory(project, query="what database are we using and why")
for d in ctx["warm"]["decisions"]:
    print(d["score"], d["what"])
# ctx["hot"]  → open tasks + latest snapshot
# ctx["warm"] → decisions ranked against the query
```

### Record what you ruled out (provenance)

```python
from hive import write_memory, get_provenance

dec = write_memory("decision", project, {
    "what": "Migrated the queue from RabbitMQ to Kafka",
    "why":  "Need partition ordering and replay for billing events"})

write_memory("dead_end", project, {
    "what_tried":         "Evaluated RabbitMQ for the event backbone",
    "why_failed":         "No native replay; ordering guarantees were per-queue only",
    "chosen_decision_id": dec["id"]})

prov = get_provenance(dec["id"])   # {decision, dead_ends[], supersedes}
```

### Let confidence age, re-affirm what's still true

```python
from hive import reinforce_decision, sweep_archive
reinforce_decision(decision_id)     # +confidence, resets the decay clock, un-archives
sweep_archive(project)              # cold-archive decisions whose decayed conf < 0.25
```

### Hand off to the next agent / route work

```python
from hive import create_handoff, route_task

packet = create_handoff(project, from_agent="claude", to_agent="next")
# packet["state"] = open tasks + snapshot + top decisions
# packet["delta"] = what changed since the previous handoff

ranked = route_task(project, "add OAuth to the public API")
# -> [{agent, score, evidence:[...]}]  (advisory only — never auto-assigns)
```

### Auto-capture decisions from git commits

```bash
python -m hive.cli.hook install          # idempotent post-commit hook (per repo)
python -m hive.cli.capture <sha>         # what the hook runs: extract → guard → write
python -m hive.cli.capture stats         # decisions at conf 1.0, by source, skip reasons
python -m hive.cli.capture calibrate 50  # LOG-ONLY pre-filter pass-rate + verdict
python -m hive.cli.hook uninstall        # removes only Hive's hook block
```

Only commits carrying decision language (`chose … over`, `switched to`, `because`, …)
clear the floor; survivors go through the **full guard** at reduced confidence (0.6),
tagged `source='git-hook'`. Sub-threshold commits are dropped and audited, never staged.

---

## How retrieval works

```
query → normalize (case-fold, stopwords, stem, synonym-expand)
      → TF-IDF overlap score (smoothed IDF, headline + recency + confidence boosts)
      → hybrid rerank: pin the top TF-IDF hit, let dense embeddings reorder the head
      → pack into a token budget (hot 500 / warm 2500)
```

- **TF-IDF** — keyword precision, set-overlap on smoothed IDF.
- **Dense** — `BAAI/bge-small-en-v1.5` (384-dim, 33 MB, ONNX via fastembed, no torch).
- **Hybrid (RRF)** — fuses the two; pins the keyword #1 (confidence-gated) and lets
  the embeddings reorder the rest of the head. The keyword anchor is what keeps it
  robust at scale where dense-alone drifts to semantically-adjacent-but-wrong docs.

### Benchmark

Measured against a labeled query/decision eval set (`tests/eval_corpus.json`):

| method               | Recall@1 | Recall@3 |   MRR |
|----------------------|----------|----------|-------|
| TF-IDF               |  74.0%   |  83.3%   | 0.803 |
| dense                |  60.4%   |  80.2%   | 0.721 |
| **hybrid (default)** | **79.2%**| **91.7%**| **0.856** |

Hybrid beats TF-IDF on every metric and every category, and **stays flat across 54×
corpus growth** (a cross-encoder reranker was evaluated and rejected — worse and
~250× slower). Run it yourself:

```bash
PYTHONIOENCODING=utf-8 python tests/bench_recall.py   # tfidf vs dense vs hybrid
PYTHONIOENCODING=utf-8 python tests/bench_scale.py    # recall + latency vs corpus size
```

---

## The write guard

Every write — human, agent, or git-hook — passes through `hive/core/guard.py`
before commit. Order matters:

1. Required fields present and non-empty
2. Not vague (decisions/tasks need ≥ 5 words in the main field)
3. Not an exact duplicate
4. **Not a contradiction** of an existing decision (opposition markers, swapped sides)
5. Not a fuzzy duplicate (Jaccard token overlap ≥ 0.45)
6. Has a `why`

A flagged write isn't dropped — it goes to a **staging** queue for human review, or
is auto-rejected only if the system *learned* that category is reliably wrong for
this project. Review staged records:

```bash
python -m hive.cli.staging list          # pending review
python -m hive.cli.staging accept <id>   # promote to the store
python -m hive.cli.staging tune          # learn auto-reject policies from history
python -m hive.cli.audit   tail          # append-only event log
```

---

## Storage

A single SQLite file (`hive.db`, override with `HIVE_DB_PATH`). 10 tables:

| Table | Role |
|---|---|
| `decisions` | committed long-term decisions (warm tier) + supersession, archive, source |
| `snapshots` | latest project structure (hot tier) |
| `open_tasks` | live work items (hot tier) |
| `dead_ends` | rejected approaches, linked to the decision that replaced them |
| `staging` | writes the guard flagged for review |
| `staging_history` | reviewer outcomes — feeds the auto-tune learner |
| `guard_policy` | per-project, per-category action (`stage` / `auto_reject`) |
| `audit_log` | append-only event stream (every write + every query) |
| `decision_embeddings` | cached float32 embeddings per decision |
| `handoffs` | persisted agent handoff packets (state + delta) |

---

## Project layout

```
hive/
  __init__.py        public API (read_memory, write_memory, get_provenance, …)
  db/setup.py        SQLite init + idempotent migrations
  core/
    guard.py         6 write-guard rules (never bypassed)
    writer.py        write_memory, close_task, reinforce/archive, staging promote
    reader.py        read_memory (hot + warm tiers), get_provenance
    normalize.py     tokeniser: stopwords, stemmer, synonym map
    dense.py         dense cosine + RRF hybrid fusion
    embedder.py      fastembed wrapper (bge-small-en-v1.5)
    decay.py         confidence decay + archive constants
    handoff.py       agent handoff packets (state + delta)
    routing.py       expertise routing (decay-aware, advisory)
    extract.py       pure commit → decision extractor (the quality floor)
    policy.py        per-project guard policy + auto-tune learner
    audit.py         append-only event log
  cli/
    staging.py  audit.py  init.py  capture.py  hook.py
tests/
  test_day1.py … test_day11.py   per-feature end-to-end tests
  bench_recall.py  bench_scale.py  bench_rerank.py  eval_corpus.json
```

---

## Running the tests

```bash
# End-to-end feature tests (all must pass before any commit)
PYTHONIOENCODING=utf-8 python tests/test_day1.py    # … through test_day11.py

# Retrieval benchmarks (must not regress below the table above)
PYTHONIOENCODING=utf-8 python tests/bench_recall.py
PYTHONIOENCODING=utf-8 python tests/bench_scale.py
```

---

## Roadmap

- **Phase 1** — Core memory, write guard, staging, audit, auto-tune. ✅
- **Phase 2** — Semantic embeddings + hybrid RRF retrieval. ✅
- **Phase 3** — Dead ends, decision provenance, idempotent agent global config. ✅
- **Phase 4** — Confidence decay, cold archive, contradiction detection v2. ✅
- **Phase 5** — Agent handoff packets, decay-aware expertise routing. ✅
- **Phase 6** — Git-commit decision extraction (quality floor + post-commit hook). ✅
- **Later** — file watcher / daemon, vectorized TF-IDF for large corpora, npm binary wrapper.

---

## Design principles

- **Never bypass the write guard** — one corrupt record poisons every future retrieval.
- **Reads are side-effect free** — decay and ranking never mutate stored data, so the
  benchmark stays honest.
- **The TF-IDF fallback must always work** — the dense path is strictly optional.
- **Staging over deletion** — deleted bad data gives no signal; every staged record
  is feedback for the learner.
- **Local-first** — no vector DB until records exceed the benchmarked crossover point.

---

## License

No license has been declared yet. Until one is added, all rights are reserved —
open an issue if you'd like to use this.
