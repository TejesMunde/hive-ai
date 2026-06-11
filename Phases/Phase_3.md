# Phase 3 — Dead Ends, Decision Provenance, Agent Global Config

**Status:** ✅ IMPLEMENTED on branch `phase-3-dead-ends` (test_day8 green; days 1–7
+ retrieval benchmark unchanged). Schema-first, per review guidance.
**Goal:** turn the memory from "what we decided" into "what we decided *and what we
ruled out and why*", and make Hive discoverable to any agent on the machine.

Three deliverables: (1) a `dead_ends` table linked to the decision that replaced
each one, (2) decision provenance (queryable "what was considered before X"),
(3) idempotent global-config injection into agent rule files.

---

## 1. Dead Ends — schema

The trap (called out in review): a flat `rejected_approaches(reason)` table is a
graveyard. The value is the **link** from a dead end to the decision that
replaced it. Without it you cannot answer "what did we try before landing here?"

```sql
CREATE TABLE IF NOT EXISTS dead_ends (
    id                  TEXT PRIMARY KEY,
    project             TEXT NOT NULL,
    what_tried          TEXT NOT NULL,   -- the approach that was rejected
    why_failed          TEXT NOT NULL,   -- concrete reason it didn't work
    chosen_decision_id  TEXT,            -- the decision that replaced it (nullable)
    agent               TEXT,
    created_at          TEXT NOT NULL,
    confidence          REAL DEFAULT 1.0,
    FOREIGN KEY (chosen_decision_id) REFERENCES decisions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_dead_ends_project ON dead_ends(project);
CREATE INDEX IF NOT EXISTS idx_dead_ends_chosen  ON dead_ends(chosen_decision_id);
```

Design notes:
- `chosen_decision_id` is **nullable**: a dead end can be recorded before its
  replacement exists (e.g. "tried X, abandoned it" with the winner still TBD).
  Backfill the link later via an update path.
- `ON DELETE SET NULL`, not CASCADE: deleting a decision must not silently erase
  the record that we explored alternatives. The dead end outlives the decision.
- The full provenance chain is `dead_end.what_tried → why_failed →
  chosen_decision_id → decisions.what/why`.

### Optional companion: decision supersession

A decision can also replace an *earlier decision* (not just a dead end) — e.g.
"switched from REST to gRPC". Model that on `decisions` itself:

```sql
ALTER TABLE decisions ADD COLUMN supersedes_id TEXT
    REFERENCES decisions(id) ON DELETE SET NULL;   -- nullable, default NULL
```

Two distinct relationships, both pointing at the winning decision:
- `dead_ends.chosen_decision_id` — a rejected *approach* → the decision.
- `decisions.supersedes_id` — a prior *decision* → the decision that replaced it.

(SQLite has no in-place `ADD COLUMN ... REFERENCES` enforcement, but the column +
app-level FK usage is fine; Phase 1 already accepts no-migration table rebuilds.)

---

## 2. Write + read paths

**Write** — fold into the existing `write_memory` as a new `record_type`
(`"dead_end"`), so it flows through the SAME guard → staging → audit pipeline.
No bypass, consistent with the Phase 1 rule.

Guard rules for `dead_end`:
- Required: `what_tried`, `why_failed` (both non-empty).
- Vague check: `what_tried` ≥ 5 words (reuse Rule 2).
- Dedup: fuzzy-Jaccard on `what_tried` within project (reuse Rule 5).
- If `chosen_decision_id` is given, validate it exists in `decisions`.

`write_memory("dead_end", project, {...})` returns the usual
`committed | staged | auto_rejected | rejected`. New audit kinds:
`write_commit` already covers it via the `type` payload field.

**Read** — dead ends are *provenance*, not task context, so they should NOT eat
the hot/warm token budget by default. Expose a dedicated call instead:

```python
get_provenance(decision_id) -> {
    "decision":   {...},                 # the decision row
    "dead_ends":  [ {what_tried, why_failed, agent, created_at}, ... ],
    "supersedes": {...} | None,          # the decision this one replaced (1 hop)
}
```

Query: `SELECT * FROM dead_ends WHERE chosen_decision_id = ?` +
`SELECT * FROM decisions WHERE id = (SELECT supersedes_id FROM decisions WHERE id = ?)`.

Optional later: a `HIVE_INCLUDE_DEADENDS=1` flag to let `read_memory` surface the
top-1 dead end for the top warm decision, within a small extra budget.

### Possible guard integration (Phase 3.5, not now)

The contradiction guard (Rule 4) already detects a flipped choice ("X over Y" vs
"Y over X"). When it fires AND the new decision is accepted, we *could*
auto-create a `dead_end` for the superseded side. Powerful, but defer — it
couples two subsystems; ship the manual path first.

---

## 3. Agent global config — idempotent init

`hive init` should make Hive discoverable to whatever agent runs next by writing a
usage block into global rule files:

| Target file | Marker syntax |
|-------------|---------------|
| `~/.claude/CLAUDE.md`      | `<!-- HIVE:BEGIN -->` … `<!-- HIVE:END -->` |
| `~/.codeium/windsurf/memories/` or `~/.windsurf/rules` | `<!-- HIVE:BEGIN -->` … |
| `~/.cursor/rules` / `~/.cursorrules` | `<!-- HIVE:BEGIN -->` … |

**Idempotency is the whole game** (review point): running `hive init` twice must
not duplicate the block.

Algorithm per target:
1. Read file (create empty if missing; `mkdir -p` the parent).
2. If both markers present → **replace** the span between them with the current
   block.
3. Else → **append** the marker-wrapped block.
4. Skip the write entirely if the existing span already byte-matches (no-op,
   stable mtime).

```
<!-- HIVE:BEGIN (auto-generated by `hive init` — edit outside these markers) -->
## Hive Mind memory
Before a task: `from hive import read_memory; read_memory(project, query=...)`
After a decision: `write_memory("decision", project, {...})`
...
<!-- HIVE:END -->
```

Notes:
- Marker comment style must match the file (Markdown `<!-- -->` works for
  CLAUDE.md; a `# HIVE:BEGIN` variant for plain rule files that don't render HTML
  comments). Pick per-target.
- Never touch content outside the markers — the user's own rules are sacred.
- A `hive init --dry-run` should print the diff per target before writing.

---

## 4. Open questions (resolve before coding)

1. **Supersession chains** — cap provenance walk at 1 hop, or follow the full
   `supersedes_id` chain? (1 hop for v1; chains risk cycles — add a guard.)
2. **Dead-end retrieval ranking** — when a query matches dead ends semantically,
   do we ever surface them in `read_memory`, or strictly on-demand via
   `get_provenance`? (Strictly on-demand for v1.)
3. **Global-config targets** — confirm the exact rule-file paths/filenames for
   Cursor and Windsurf on each OS before writing to them.
4. **Embeddings for dead ends** — do dead ends get embedded into
   `decision_embeddings` too (so "why did we avoid X" is retrievable)? Likely
   yes, but as a separate `kind` column or table — decide before wiring.

---

## 5. Acceptance criteria (proposed)

- [x] `dead_ends` table + `decisions.supersedes_id` created idempotently in `setup.py`
- [x] `write_memory("dead_end", ...)` flows through the guard (no bypass) + audit
- [x] `get_provenance(decision_id)` returns the decision, its dead ends, and the
      decision it superseded
- [x] Deleting a decision nulls the links, never erases dead ends
- [x] `hive init` writes the Hive block to global rule files; running it twice is
      a no-op (no duplication)
- [x] `test_day8.py` covers: dead-end write+guard, provenance round-trip,
      delete-nulls, idempotent re-init
- [x] Retrieval benchmark unchanged (dead ends must not pollute decision recall)
