# Hive Mind — Agent Instructions

This file is read automatically by Claude Code at the start of every session.
Do not delete it. Do not move it. Update it as the project evolves.

---

## What This Project Is

Hive Mind is a persistent memory and continuity system for AI coding agents.
It captures decisions, open tasks, and architecture choices so any agent can
pick up exactly where the last one left off — with zero re-explanation.

The core problem: making AI-assisted development feel continuous across agent
sessions regardless of which agent is used.

---

## Project Structure

```
H.I.V.E/
  hive/
    __init__.py          ← public API: init_db, get_connection, write_memory,
                            read_memory, get_provenance, close_task,
                            promote/reject_from_staging
    db/setup.py          ← SQLite init, 10 tables, migrations, get_connection()
    core/
      guard.py           ← 6 write-guard rules, validates every record pre-commit
      writer.py          ← write_memory, close_task, promote/reject staging
      reader.py          ← read_memory: hot + warm tiers, IDF rank + hybrid rerank; get_provenance
      normalize.py       ← case-fold, stopword drop, suffix stemmer, synonym map
      dense.py           ← dense retrieval + RRF hybrid (pin TF-IDF #1 + top-K head)
      embedder.py        ← fastembed wrapper (bge-small-en-v1.5, 384-dim)
      policy.py          ← per-project guard_policy + tune_policies() learner
      audit.py           ← append-only event log (read/aggregate helpers)
      decay.py           ← Phase 4: confidence decay + archive constants (pure)
      handoff.py         ← Phase 5: handoff packets (state + delta-since-last)
      routing.py         ← Phase 5: expertise routing (decay-aware relevance)
      extract.py         ← Phase 6: PURE commit→decision extractor (quality floor)
    cli/
      staging.py         ← staging review CLI: list/accept/reject/clear/stats/tune/review
      audit.py           ← audit log CLI: tail/counts/fails
      init.py            ← `hive init`: idempotent global-config injection (Phase 3)
      capture.py         ← Phase 6: `hive capture <sha>` git-hook edge + `stats`
      hook.py            ← Phase 6: idempotent post-commit hook install/uninstall
  tests/
    test_day1.py … test_day8.py   ← per-day end-to-end tests
    bench_recall.py      ← tfidf vs dense vs hybrid: Recall@1/@3, MRR, latency
    bench_rerank.py      ← cross-encoder rerank delta over hybrid
    bench_scale.py       ← recall + latency vs corpus size (K distractors)
    bench_vector.py      ← vector-store microbench
    eval_corpus.json     ← labeled query/decision eval set (from gen_eval_corpus.py)
    gen_eval_corpus.py   ← regenerates eval_corpus.json
    diag_fails.py        ← per-query failure diagnostics
  Phases/Phase_1.md      ← full Phase 1 build log + acceptance record
  hive.db                ← live SQLite store
```

> `ruvector.db` (~258 MB) sits at repo root but is NOT referenced by any code in
> `hive/`. Treat as a stray artifact / abandoned experiment. Confirm with the
> user before deleting — it is large and may be intentional.

---

## Current Phase

**Phase 1 — SHIPPED.** Days 1–7 complete and tested. Milestone met:
`tests/test_day7.py` → 15/15 top-1, MRR 1.000, no token-budget breach. Full
build log in `Phases/Phase_1.md`.

**Phase 2 — SHIPPED (semantic retrieval).** Full build log in `Phases/Phase_2.md`.
- `core/embedder.py` — fastembed ONNX, model `BAAI/bge-small-en-v1.5` (384-dim,
  L2-normalized so cosine = dot product). Loaded lazily, cached per-process.
- `core/dense.py` — dense cosine ranking + RRF hybrid (`fuse_and_guard`): pins
  the top TF-IDF hit, fuses dense across the rest of the top-K head. No negation
  guard (removed — was net-harmful; see baseline).
- `reader.py` calls `hybrid_rerank` when a real query is present. Dense path is
  optional: falls back silently to TF-IDF ordering if `fastembed` is missing,
  `HIVE_DENSE=0`, or any embed/cache error.
- Embeddings cached in the `decision_embeddings` table (float32 BLOB per decision).

Milestone met: **hybrid beats the TF-IDF baseline on every metric** (79.2/91.7/0.856
vs 74.0/83.3/0.803) and holds that lead across 54× corpus growth (`bench_scale.py`);
cross-encoder evaluated and rejected.

**Phase 3 — IMPLEMENTED on branch `phase-3-dead-ends` (pending merge).** Design +
build notes in `Phases/Phase_3.md`.
- `dead_ends` table + `decisions.supersedes_id`, added idempotently in `setup.py`
  (PRAGMA-checked `ALTER` migrates pre-Phase-3 DBs).
- `write_memory("dead_end", …)` flows through the guard (no bypass); dangling
  `chosen_decision_id` / `supersedes_id` rejected.
- `get_provenance(decision_id)` → decision + its dead ends + 1-hop supersession,
  on-demand (NOT in the hot/warm budget — retrieval benchmark unmoved).
- `python -m hive.cli.init` injects an idempotent, marker-wrapped Hive block into
  global agent rule files (`~/.claude/CLAUDE.md`, …) — re-running never duplicates.
- `tests/test_day8.py` green; days 1–7 + `bench_recall` unchanged.

**Phase 4 — IMPLEMENTED on branch `phase-4-decay-archive` (pending merge).** Design +
build notes in `Phases/Phase_4.md`.
- **Confidence decay** (`core/decay.py`): `eff_conf = stored × 0.5^(age_days/90)`,
  computed at read time — stored `confidence` is NEVER mutated by reads. age 0 →
  eff == stored, so the retrieval benchmark is unmoved. Reader's confidence nudge
  now uses the decayed value; `read_memory` exposes `effective_confidence`.
- **`reinforce_decision(id)`**: bumps stored confidence (+0.25, capped at 1.0),
  resets `created_at` (the decay clock), and un-archives.
- **Cold archive** (`decisions.archived_at`, idempotent migration): a decision
  leaves the warm tier when superseded (auto), when `sweep_archive()` finds its
  eff_conf below `ARCHIVE_FLOOR=0.25`, or via explicit `archive_decision(id)`.
  `read_memory` excludes archived by default; `include_archived=True` surfaces
  them; `get_provenance` always resolves them.
- **Contradiction detection v2** (`guard._find_contradiction_dense`): adds a dense
  path — high cosine (`CONTRA_SIM=0.80`) + shared subject + an opposition/
  replacement cue → flag to staging. 0 false positives on the eval corpus;
  degrades to the v1 swapped-noun heuristic when dense is off.
- `tests/test_day9.py` green; days 1–8 + `bench_recall` unchanged (79.2/91.7/0.856).

**Phase 5 — IMPLEMENTED on branch `phase-5-handoff-routing` (pending merge).** Design +
build notes in `Phases/Phase_5.md`.
- **Handoff packets** (`core/handoff.py`, `handoffs` table): `create_handoff(project,
  from_agent, to_agent)` persists a packet = current `state` (open tasks + snapshot +
  top decisions, via `read_memory`) + `delta` since the *previous* handoff
  (decisions/dead_ends added, tasks opened/closed). First handoff → `since=None`,
  full history; consecutive no-activity handoff → empty delta. `get_handoff` /
  `latest_handoff` read packets back. `open_tasks.closed_at` added (migration) so
  the delta can report tasks closed *within* an interval.
- **Expertise routing** (`core/routing.py`): `route_task(project, task)` ranks agents
  by `Σ relevance(task, their live decisions) × effective_confidence` — IDF-overlap
  (+ optional dense blend), decay-aware so fresh expertise outranks stale. Advisory
  only: returns ranked agents + evidence decisions, **never mutates / auto-assigns**.
- `tests/test_day10.py` green; days 1–9 + `bench_recall` unchanged (Phase 5 adds no
  retrieval-path change).

**Phase 6 — IMPLEMENTED on branch `phase-6-git-capture` (pending merge).** Design +
build notes in `Phases/Phase_6.md`. First **machine write path** — quality floor first.
- **Pure extractor** (`core/extract.py`): `parse_commit(raw)` → `CommitInfo`;
  `extract_decision(info)` → `Candidate | Skip`. No git, no DB — the hardest piece is
  the most testable. Three gates: (1) type — conventional `{feat,fix,refactor,perf}`
  or a ≥5-word prefix-less subject; merges / `chore|docs|style|test|build|ci` / version
  bumps skipped; (2) decision-cue — message must carry decision language (`chose`,
  ` over `, `switched to`, `because`, `instead of`, …), aligned with the guard's
  `_REPLACE_CUES`; (3) substance — `what` ≥ 5 words AND a real `why` exists, so a
  survivor also clears the guard on its own merits. Tuned for PRECISION over recall.
- **Capture edge** (`cli/capture.py`): `hive capture <sha>` shells `git show -s` for the
  message only, runs the extractor. A `Skip` → audit `extract_skipped` + exit (the floor;
  **nothing reaches staging**). A `Candidate` → the NORMAL `write_memory(..., source=
  'git-hook', confidence=0.6)` — **guard never bypassed** — then a snapshot refresh.
  **No handoff write** (delta-explosion guard). `MACHINE_CONFIDENCE=0.6` so machine
  decisions rank below confirmed human ones until reinforced (no auto-reinforce).
- **Hook installer** (`cli/hook.py`): `install`/`uninstall`/`status` write a marker-
  wrapped `.git/hooks/post-commit` — idempotent, append-safe over an existing hook,
  uninstall removes only Hive's block. Best-effort: a capture failure never blocks a commit.
- **Provenance** (`decisions.source`, `staging.source`; idempotent migration): tags writes
  `git-hook` / `human-reviewed` / NULL(agent). Observability only — does NOT change
  ranking. `hive capture stats` reports cap-saturation (decisions at exactly 1.0), counts
  by source, and `extract_skipped` reasons.
- `tests/test_day11.py` green (pure-gate unit tests + end-to-end through the real guard);
  days 1–10 + `bench_recall` unchanged (79.2/91.7/0.856 — machine writes are age-0 at
  conf 0.6; eval corpus is all conf-1.0, so the benchmark is unmoved).

---

## DependenciesPhase 1 was pure stdlib. **Phase 2 adds `numpy` and `fastembed`** (ONNX runtime).
These are required for the dense/hybrid retrieval path. Pure-stdlib code paths
must still work when `fastembed` is absent (reader degrades to TF-IDF).

---

## Accuracy Benchmark — Read This Before Touching reader.py / dense.py / normalize.py

Retrieval quality is measured against `tests/eval_corpus.json`.

```bash
# from repo root
PYTHONIOENCODING=utf-8 python tests/bench_recall.py    # tfidf vs dense vs hybrid
PYTHONIOENCODING=utf-8 python tests/bench_rerank.py    # + cross-encoder rerank delta
python tests/gen_eval_corpus.py                        # regenerate eval corpus
python tests/diag_fails.py                             # per-query failure diagnostics
```

### Baseline — 38 docs / 96 queries (recorded 2026-06-10)

All methods rank through the SAME production core
(`hive.core.dense.fuse_and_guard`); `bench_recall` and `bench_rerank` agree on
hybrid exactly.

| method                | Recall@1 | Recall@3 |   MRR | p50_ms |
|-----------------------|----------|----------|-------|--------|
| tfidf                 |  74.0%   |  83.3%   | 0.803 |  0.03  |
| dense                 |  60.4%   |  80.2%   | 0.721 |  0.01  |
| **hybrid (default)**  | **79.2%**| **91.7%**| **0.856** | 0.10 |
| hybrid + cross-encoder|  69.8%   |  84.4%   | 0.779 | 27.1   |

**Hybrid wins — beats TF-IDF on every metric and every category** (exact 100/100,
negation R@3 100, paraphrase 85.7/92.9, vocab_gap 69.0/88.1). `HIVE_DENSE=1` (the
reader default) is correct: keep dense ON.

Three fixes got it there (all in `dense.py`):
1. **Fusion stops diluting exact hits.** `fuse_and_guard` keeps dense fusion to the
   TF-IDF top-K (`FUSE_TOP_K=10`) head; the tail is kept verbatim. Full-corpus RRF
   used to crash exact Recall@1 from 100% → 70%.
2. **The negation guard was removed — net-harmful.** It demoted any doc sharing a
   token with the words after `not/no/without/...`, burying correct docs (e.g.
   "rejected django" buried the django decision). Off: hybrid R@1 69.8% → 76.0%,
   R@3 85.4% → 90.6%. If a "why NOT X" feature is ever wanted, build a precise
   version (target only the single chosen-X doc) — never token-overlap demotion.
3. **The rank-0 pin is confidence-gated** (`PIN_MARGIN=0.15`). Pinning the TF-IDF
   #1 always protected exact but blocked thin-overlap paraphrase queries (correct
   doc stuck at rank 2). Pin only when TF-IDF #1 beats #2 by ≥0.15 normalized
   overlap; otherwise dense reorders the whole head. +3.2 R@1 (76.0 → 79.2),
   exact still 100. The remaining weak spot is `vocab_gap` Recall@1 (69%) — the
   zero-keyword-overlap tail where dense alone must carry (model-quality bound).

**Cross-encoder rerank stays rejected** — worse on every metric and ~250× slower
(0.10ms → 27ms p50); zeroes `negation` Recall@1. `bench_rerank.py` keeps it only
as evidence; do NOT wire it into the reader.

### Embedding model A/B (recorded 2026-06-11)

Tested bigger encoders through the gated-pin hybrid:

| model     | size / dim   | dense R@1 | hybrid R@1/R@3/MRR | vocab_gap R@1 |
|-----------|--------------|-----------|--------------------|---------------|
| bge-small | 33 MB / 384  | 60.4%     | **79.2 / 91.7 / 0.856** | 30/42    |
| bge-base  | 220 MB / 768 | 69.8%     | 78.1 / 91.7 / 0.855 | 29/42         |
| bge-large | 1.3 GB / 1024| 75.0%     | 81.2 / 93.8 / 0.878 | 31/42         |

- **Keep bge-small.** `bge-base` lifts dense-alone but gives the hybrid *nothing*
  (the gated pin + TF-IDF already capture it) at 2× storage. `bge-large` adds
  +2 R@1 but at 40× model size / 2.7× per-vector storage / ~3× slower embed —
  not worth it for a local-first, zero-setup tool.
- The `vocab_gap` Recall@1 tail (~71%) barely moves even at bge-large (30→31/42):
  it is **not** a model-capacity bound. Those short zero-keyword-overlap queries
  need targeted `_SYNONYMS` entries, not a bigger encoder.
- `all-mpnet-base-v2` is **not** supported by fastembed (ValueError) — bge is the
  ONNX-available family. bge-large is a documented opt-in for a future
  large-corpus phase, never the default.

**Rule: never merge a change to `reader.py`, `dense.py`, or `normalize.py` that
drops hybrid Recall@1 / Recall@3 / MRR below the table above.** Re-run both
benches, paste the new numbers, then change code.

### Scale — `bench_scale.py` (labeled set + K synthetic distractors)

Floods the corpus with disjoint-vocabulary distractor decisions and re-runs the
96 labeled queries at K = 0…2000.

| corpus | hybrid R@1/R@3/MRR | dense R@1/R@3 | hybrid fuse p50 | tfidf p50 |
|--------|--------------------|---------------|-----------------|-----------|
| 38     | 79.2 / 91.7 / 0.856| 60.4 / 80.2   | 0.03 ms         | 0.03 ms   |
| 2038   | 79.2 / 89.6 / 0.841| 49.0 / 58.3   | 1.09 ms         | 1.26 ms   |

- **Hybrid recall is flat across 54× corpus growth** — the fix is not overfit to
  the small corpus.
- **Dense-alone collapses with scale** (R@1 60→49%, R@3 80→58%) as it drifts to
  semantically-adjacent distractors. Hybrid is immune because the pinned TF-IDF #1
  anchors the top result. **Never ship dense-only.**
- **Scaling bottleneck is the TF-IDF pure-Python loop, not dense** (1.2 ms vs
  0.2 ms p50 at ~2k docs). Vectorize TF-IDF before pushing past ~10k decisions.

### Retrieval pipeline (current)

```
query → split → lowercase → strip punct → drop stopwords → stem → synonym expand
      → IDF overlap score per decision
        + 25% headline boost (hit in `what` vs only `why`)
        + up to +0.05 recency (only when base overlap > 0)
        + (confidence - 1.0) × 0.05
      → sort score desc, created_at desc
      → if real query: hybrid_rerank — pin TF-IDF #1, dense re-orders top-K head
      → pack into warm 2500-token budget → emit `query` event to audit_log
```

- **TF-IDF** smoothed IDF: `log((N+1)/(df+1)) + 1`, set-overlap scoring.
- **Dense** bge-small cosine over cached embeddings.
- **Hybrid** (`dense.fuse_and_guard`) RRF `score = Σ 1/(60 + rank)`, constant
  **60** standard — do not change without re-running the bench. Pins the top
  TF-IDF hit at rank 0, fuses dense over the rest of the top-K head
  (`FUSE_TOP_K=10`), keeps the TF-IDF tail verbatim. Zero corpus overlap → dense
  ranks alone. No negation guard (removed — see baseline).

### normalize.py — the stemmer + synonym map

`core/normalize.py` is the single source of truth for tokenisation. Conservative
suffix stemmer (`-ies/-ied/-sses/-ion/-ing/-edly/-ingly/-ed/-ly/-s`, with a
silent-e rule: `cached → cache`). `_SYNONYMS` emits canonical tags ALONGSIDE the
original token (never replaces it), e.g. `sqlite/postgres → database`,
`fastapi/flask → framework, api`, `monitor → track`. Keep the map small — every
entry is a potential false positive. Add an entry only with a test behind it.

---

## Write Guard Rules — Do Not Bypass

Every write goes through `hive/core/guard.py` before touching the DB.
Order matters (Rule 4 runs before Rule 5 on purpose):

1. Required fields present and non-empty
2. Not vague — decisions/tasks need ≥ 5 words in the main field
3. Exact duplicate (decision `what` / task `description`) → flagged
4. **Contradiction** — same nouns around an opposition marker (`over`, `vs`,
   `instead of`, `not`, `rather than`) with the sides swapped → flagged.
   Runs BEFORE fuzzy-dup so a flipped-choice reword is not mislabeled a duplicate.
5. Fuzzy duplicate — Jaccard token overlap ≥ 0.45 → flagged
6. Missing `why` on a decision → flagged

A flagged write does NOT get silently dropped. The writer looks up
`guard_policy(project, category)`:
- `stage` (default) → record goes to the `staging` table for human review
- `auto_reject` → dropped outright (only after the learner has seen this category
  rejected ≥ 5 times with ≤ 10% accept rate, per project)

`write_memory` returns one of: `committed | staged | auto_rejected | rejected`.
Review staged records: `python -m hive.cli.staging list`

Record types: `decision | snapshot | open_task | dead_end`. A `dead_end` needs
`what_tried` + `why_failed` (same vague + fuzzy-dup checks); a dangling
`chosen_decision_id` / `supersedes_id` is rejected outright — the referenced
decision must exist.

**Never add a bypass flag or skip the guard for any write path, including tests.**
One corrupt record poisons every future agent call that retrieves it.

---

## Storage — SQLite, 10 tables

| Table | Role |
|---|---|
| `decisions`           | committed long-term decisions (warm tier); `supersedes_id` → prior decision; `archived_at` → cold-archive flag (Phase 4); `source` → write provenance (Phase 6) |
| `snapshots`           | latest project structure (hot tier) |
| `open_tasks`          | live work items (hot tier) |
| `dead_ends`           | rejected approaches; `chosen_decision_id` → the decision that replaced it (Phase 3) |
| `staging`             | writes the guard flagged for review; `source` → provenance tag (Phase 6) |
| `staging_history`     | reviewer outcomes — feeds the auto-tune learner |
| `guard_policy`        | per-project per-category action (`stage`/`auto_reject`); PK `(project, category)` |
| `audit_log`           | append-only event stream — every write + every query |
| `decision_embeddings` | cached float32 embeddings per decision (model + dim + BLOB) |
| `handoffs`            | persisted agent handoff packets (state + delta JSON); delta boundary = prior handoff (Phase 5) |

`PRAGMA foreign_keys = ON`. Row factory `sqlite3.Row`. DB path overridable via
env `HIVE_DB_PATH` (default `hive.db`).

---

## Key Architectural Decisions

| Decision | Why |
|---|---|
| SQLite for now | Zero setup, file-based, survives `git clone`. Migrate when scale demands |
| Jaccard over SequenceMatcher | Concept overlap beats character similarity for dedup |
| Staging over deletion | Deleted bad data gives no signal. Every staged record is feedback |
| Auto-reject learned per `(project, category)` | Per-project. A global PK once poisoned other projects (caught Day 5) |
| `BAAI/bge-small-en-v1.5` for embeddings | 384-dim, 33 MB, MIT, MTEB 62.2. ONNX via fastembed — no torch. (Supersedes the earlier `all-mpnet-base-v2` plan) |
| RRF fusion of TF-IDF + dense | Combines keyword precision with semantic recall; constant 60 standard |
| Pin TF-IDF #1 + top-K dense fusion | Full-corpus RRF diluted exact hits (R@1 100% → 70%). Pinning + head-only fusion makes hybrid beat TF-IDF on all metrics |
| Removed the negation guard | Token-overlap demotion buried correct docs (−6 pts R@1). Revisit only with a precise single-doc version |
| Audit log append-only, INTEGER PK | Replayable, never mutated, survives migration |
| Dead ends linked to the chosen decision (`ON DELETE SET NULL`) | A flat rejected-approaches table is a graveyard; the link makes it queryable provenance. SET NULL so the dead end outlives the decision |

---

## What NOT to Do

- Do not add a vector DB until records exceed the benchmarked crossover point —
  RRF over cached numpy vectors is fine at current scale
- Do not skip the write guard for any write path, including tests
- Do not change the token budgets (hot 500 / warm 2500) without re-running the bench
- Do not change the RRF constant (60) or the fuzzy threshold (0.45) without a bench run
- Do not break the TF-IDF fallback — the dense path must stay optional
- Do not publish to npm yet — deferred to a later phase as a binary wrapper

---

## How to Run Tests

```bash
# Per-day end-to-end tests (run all before any commit)
PYTHONIOENCODING=utf-8 python tests/test_day1.py
# … through …
PYTHONIOENCODING=utf-8 python tests/test_day8.py   # Phase 3: dead ends + provenance
PYTHONIOENCODING=utf-8 python tests/test_day9.py   # Phase 4: decay + archive + contradiction v2
PYTHONIOENCODING=utf-8 python tests/test_day10.py  # Phase 5: handoff packets + expertise routing
PYTHONIOENCODING=utf-8 python tests/test_day11.py  # Phase 6: git-commit decision extraction

# Retrieval benchmarks
PYTHONIOENCODING=utf-8 python tests/bench_recall.py
PYTHONIOENCODING=utf-8 python tests/bench_rerank.py
PYTHONIOENCODING=utf-8 python tests/bench_scale.py   # recall + latency vs corpus size

# Staging review + policy
python -m hive.cli.staging list   [--project P]
python -m hive.cli.staging accept <id-prefix>
python -m hive.cli.staging reject <id-prefix>
python -m hive.cli.staging stats  [--project P]
python -m hive.cli.staging tune   [--project P]
python -m hive.cli.staging review [--project P]

# Audit log
python -m hive.cli.audit tail     [--project P] [--limit N]
python -m hive.cli.audit counts   [--project P]
python -m hive.cli.audit fails    [--project P]
```

All `test_dayN.py` must pass before any commit. The benchmark must not regress.

---

## Memory Usage — How Agents Should Use Hive

Before starting any task:
```python
from hive import read_memory
context = read_memory(project="hive-api", query="<what you are about to work on>")
# context["hot"]  → open_tasks + latest_snapshot (immediate task context)
# context["warm"] → ranked decisions (architectural context)
```

After completing any task:
```python
from hive import write_memory
write_memory("decision", "hive-api", {
    "what":  "what was decided, specifically (≥ 5 words)",
    "why":   "why this approach over alternatives",
    "agent": "claude-code",
})
```

When a task is done:
```python
from hive.core.writer import close_task
close_task(task_id)
```

When you rule an approach out (Phase 3) — record it linked to what you chose:
```python
from hive import write_memory, get_provenance
write_memory("dead_end", "hive-api", {
    "what_tried":         "what was attempted (≥ 5 words)",
    "why_failed":         "why it didn't work",
    "chosen_decision_id": decision_id,   # the decision that replaced it (optional)
})
# Later: "what did we consider before this decision?"
prov = get_provenance(decision_id)   # {decision, dead_ends[], supersedes}
```

Confidence ages out (Phase 4) — re-affirm a decision still in force, archive stale ones:
```python
from hive import reinforce_decision, archive_decision, sweep_archive
reinforce_decision(decision_id)      # +confidence, resets decay clock, un-archives
archive_decision(decision_id)        # explicit cold-archive (also auto on supersede)
sweep_archive(project="hive-api")    # archive decisions whose decayed conf < 0.25
# stale/superseded decisions drop out of read_memory unless include_archived=True
```

Handing off to the next agent / routing work (Phase 5):
```python
from hive import create_handoff, latest_handoff, route_task
packet = create_handoff("hive-api", from_agent="claude-code", to_agent="next")
# packet["state"] = open tasks + snapshot + top decisions
# packet["delta"] = what changed since the previous handoff
prev = latest_handoff("hive-api")               # most recent packet

ranked = route_task("hive-api", "add rate limiting to the public API")
# -> [{agent, score, evidence:[{decision_id, what, relevance}]}] ; advisory only
```

Auto-capturing decisions from git commits (Phase 6) — quality floor, not a bypass:
```bash
python -m hive.cli.hook install            # idempotent post-commit hook (per repo)
python -m hive.cli.capture <sha>           # what the hook runs; extract → guard → write
python -m hive.cli.capture stats           # decisions at conf 1.0, by source, skip reasons
python -m hive.cli.capture calibrate 50    # LOG-ONLY pre-filter pass-rate + verdict (no writes)
python -m hive.cli.hook uninstall          # removes only Hive's hook block
```
Only commits carrying decision language (`chose … over`, `switched to`, `because`, …)
clear the floor; cues are `\b`-anchored so code identifiers (`chosen_decision_id`)
don't misfire. Survivors go through the FULL guard at confidence 0.6, tagged
`source='git-hook'`. Sub-threshold commits are dropped + audited, never staged. The
hook writes decisions + a snapshot only — never a handoff. **Post-merge task:** once a
repo has ~50 normal commits, run `capture calibrate 50` and act on the verdict
(`TOO_BROAD` >40% → tighten cues; `OK` 15–40% → validated). Calibration on *this*
repo is `TOO_NARROW` by construction (10 squashed phase commits) — see `Phase_6.md`.
---## Roadmap

- **Phase 1** — Core memory, write guard, staging, audit, auto-tune. ✅ Shipped.
- **Phase 2** — Semantic embeddings (`bge-small-en-v1.5`), hybrid RRF retrieval.
  ✅ Shipped. Hybrid beats TF-IDF on every metric and holds across 54× corpus
  growth; cross-encoder evaluated and rejected. Log in `Phases/Phase_2.md`.
- **Phase 3** — Dead ends table, decision provenance, idempotent agent global
  config. ✅ Implemented + tested on branch `phase-3-dead-ends` (test_day8 green;
  retrieval benchmark unmoved). Design log in `Phases/Phase_3.md`.
- **Phase 4** — Confidence decay (lazy half-life), cold archive, contradiction
  detection v2. ✅ Implemented + tested on branch `phase-4-decay-archive`
  (test_day9 green; retrieval benchmark unmoved). Design log in `Phases/Phase_4.md`.
- **Phase 5** — Agent handoff packets (persisted state + delta), expertise routing
  (decay-aware, advisory). ✅ Implemented + tested on branch `phase-5-handoff-routing`
  (test_day10 green; retrieval benchmark unmoved). Design log in `Phases/Phase_5.md`.
- **Phase 6** — Git-commit decision extraction: pure keyword-cue extractor (quality
  floor BEFORE the guard), idempotent post-commit hook, `source` provenance, reduced
  machine confidence (0.6), cap-saturation observability. ✅ Implemented + tested on
  branch `phase-6-git-capture` (test_day11 green; days 1–10 + retrieval unmoved).
  Design log in `Phases/Phase_6.md`. Deferred to a later phase: file watcher / daemon.

*Last updated: Phase 6 implemented on `phase-6-git-capture` — git-commit decision
extraction (quality floor first); test_day11 green, days 1–10 + retrieval unchanged
(79.2/91.7/0.856). Pending merge to main.*