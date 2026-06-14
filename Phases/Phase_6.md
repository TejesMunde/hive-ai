# Phase 6 — Automated capture: git-commit decision extraction

**Status: IMPLEMENTED + TESTED.** Branch `phase-6-git-capture` (PR #5). Calibration
on this repo returned `TOO_NARROW` (pathological sample — see Calibration below);
re-run after ~50 normal commits is a documented post-merge task.

Phase 6 is where Hive stops depending on an agent *choosing* to call `write_memory`
and starts capturing decisions from the work itself. That makes it the first
**machine write path** in the system — and the first place the write guard faces
volume it was never stress-tested against. The whole phase is built quality-floor
first, automation second (per review guidance).

---

## The four failure modes this phase must not trip

Recorded up front so every design choice can be checked against them:

1. **Staging flooding.** A machine that writes on every commit can bury the human
   review queue under low-signal noise. → A **pre-filter runs *before* the guard**;
   sub-threshold candidates are dropped + audit-logged, they never reach staging.
2. **Confidence cap pressure.** Phase 4 caps confidence at 1.0. Machine writes must
   not arrive pre-maxed and crowd the rank. → Machine decisions are written at a
   **reduced confidence (`MACHINE_CONFIDENCE = 0.6`)**; only human reinforcement
   lifts them. New observability: count decisions sitting at exactly 1.0.
3. **Delta explosion.** A handoff-per-commit would make `delta-since-last-handoff`
   meaningless. → **The hook writes decisions + snapshot ONLY. Handoffs stay
   agent-initiated.** (resolved fork)
4. **Commit-parser difficulty.** The parser is the hardest, lowest-precision piece.
   → It is **keyword-cue gated** (resolved fork): a commit yields a candidate only
   when its message carries explicit decision language. High precision over recall —
   we would rather miss a decision than flood memory with diffs.

---

## Resolved forks (AskUserQuestion, 2026-06-14)

| Fork | Decision |
|---|---|
| What does the hook write? | **Decisions + snapshot only.** Handoffs remain `create_handoff`-initiated. |
| Machine quality floor | **Pre-filter *before* the guard + a `source` provenance tag.** Survivors carry `source='git-hook'` so staging review and the learner can separate machine from human. |
| Pre-filter signal | **Keyword/decision-language cues only** — the same opposition/replacement cues the contradiction guard already knows (`chose … over`, `switched to`, `because`, `instead of`, `rather than`, `replaced … with`). Narrowest, highest-precision option. |

---

## Architecture

```
git commit ──► .git/hooks/post-commit ──► python -m hive.cli.capture <sha>
                                               │
                          ┌────────────────────┴───────────────────┐
                          │  hive/core/extract.py  (PURE, no I/O)   │
                          │  parse_commit(raw) → CommitInfo         │
                          │  extract_decision(info) → Candidate|None│  ← keyword-cue gate
                          └────────────────────┬───────────────────┘
                                               │ Candidate or None
                          ┌────────────────────┴───────────────────┐
                          │  hive/cli/capture.py                    │
                          │  None  → audit 'extract_skipped', exit  │  ← quality floor
                          │  Cand. → write_memory("decision", …,    │
                          │            source='git-hook',           │
                          │            confidence=0.6)              │  ← guard still runs
                          │  + refresh snapshot from tree           │
                          └─────────────────────────────────────────┘
```

- **`extract.py` is pure** — `parse_commit(raw_text)` and `extract_decision(info)`
  take strings/dicts and return dataclasses or `None`. No git calls, no DB. This is
  the hardest component, so it is the most testable: every cue and every skip path
  has a unit test, no subprocess needed.
- **`cli/capture.py`** is the only impure edge: it shells `git show` for one sha,
  feeds the text to `extract.py`, and on a surviving candidate calls the *normal*
  `write_memory` — the guard is **never bypassed**. The pre-filter is an *additional*
  floor in front of the guard, not a replacement for it.
- **Hook install** extends the existing idempotent-injection pattern from
  `cli/init.py`: `python -m hive.cli.hook install` writes a marker-wrapped
  `.git/hooks/post-commit` (append-safe if one already exists), `--uninstall`
  removes only Hive's block. Never clobbers a user's existing hook.

---

## The pre-filter (quality floor) — `extract.py`

A commit becomes a decision candidate **only if all hold**:

1. **Type gate.** Conventional-commit prefix is in `{feat, fix, refactor, perf}` —
   OR (no prefix) the subject is ≥ 5 words. `chore/docs/style/test/build/ci`,
   merge commits, and version-bump subjects are skipped outright.
2. **Decision-cue gate.** Message body (or subject) contains at least one cue from
   `_DECISION_CUES`, matched as `\b`-anchored regex (so code identifiers like
   `chosen_decision_id` do NOT count) — reusing the opposition/replacement vocabulary
   the guard's contradiction detector trusts: `chose`, `over`, `switched to`,
   `instead of`, `rather than`, `because`, `replaced`, `migrated to`, `decided to`,
   `opted for`, `in favor of`, `adopted`.
3. **Substance gate.** The extracted `what` is ≥ 5 words (so it clears the guard's
   vagueness rule on its own merits — we do not want machine writes to fail the
   guard *en masse* and become staging noise; the floor should pass only writes the
   guard will also accept).

`what` = the cleaned subject line (prefix stripped). `why` = the first body
paragraph if present, else the cue sentence. If gate 3 can't find a ≥5-word `why`,
the candidate is dropped (a decision with no real "why" is exactly what the guard
rejects — drop it at the floor instead of flooding staging).

Skipped commits emit a single audit event `extract_skipped` with the reason
(`type_gate` / `no_cue` / `too_thin`). That keeps the floor observable — we can run
`hive audit counts` after a week and see how many commits the floor dropped and why.

---

## Schema changes (idempotent migration)

- `decisions.source TEXT` — provenance. `NULL`/`'agent'` = written via API,
  `'git-hook'` = machine-extracted, `'human-reviewed'` = promoted from staging.
  Added in `_migrate()` (PRAGMA-checked ALTER), index `idx_decisions_source`.
- `staging.source TEXT` — same tag carried onto staged rows so the reviewer CLI can
  show `[git-hook]` and the learner can later split accept-rates by source.

`source` is **observability + future-routing only** in this phase. It does NOT change
retrieval ranking — age-0 machine decisions at confidence 0.6 get the normal
`(confidence-1.0)×0.05` nudge (a small *negative* nudge vs a 1.0 human decision),
which is the intended "machine writes rank slightly below confirmed human ones until
reinforced." The labeled eval corpus is all confidence-1.0 → **benchmark unmoved**.

---

## Observability — confidence-cap pressure

New CLI: `python -m hive.cli.capture stats [--project P]` reports:
- count of live decisions at exactly `confidence == 1.0` (cap saturation),
- count by `source`,
- `extract_skipped` counts by reason (from audit_log).

This is the metric the review flagged: "log how many decisions are sitting at
exactly 1.0." If that number climbs under auto-capture, the cap or the reinforce
step needs revisiting — but we *measure* before we tune.

---

## What this phase deliberately does NOT do

- **No file watcher / daemon.** Post-commit hook only — synchronous, no background
  process, no new dependency. A watcher is a later phase if the hook proves out.
- **No handoff writes from the hook** (delta-explosion guard).
- **No auto-reinforcement** of machine decisions — they age and decay like any
  other; only a human (or an explicit agent call) reinforces. Avoids the cap-pressure
  feedback loop.
- **No guard bypass.** The pre-filter is *additive*. Every surviving candidate still
  passes all 6 guard rules.

---

## Test plan — `tests/test_day11.py`

Pure-extractor unit tests (no git):
- `feat: switched from REST to gRPC because latency` → candidate, what/why populated.
- `chore: bump deps` → skipped (`type_gate`).
- `fix: typo` → skipped (`too_thin` / no cue).
- `Merge branch 'x'` → skipped.
- conventional `perf:` with a real cue → candidate.
- a candidate that the *guard* would still reject (e.g. dup) → goes to staging with
  `source='git-hook'`, NOT committed (proves guard still runs over machine writes).

End-to-end (temp DB, monkeypatched `git show` text — still through real
`write_memory`):
- machine decision lands at confidence 0.6, `source='git-hook'`.
- `capture stats` reports source counts + the count-at-1.0 metric.

Regression gate (unchanged, must stay green):
- days 1–10, `bench_recall` → **79.2 / 91.7 / 0.856**.

---

## Calibration — pre-filter precision on real history

The pre-filter was run **log-only** (writes nothing) against this repo's real commit
history via the committed tool `python -m hive.cli.capture calibrate [N]`, which
reports pass-rate + skip breakdown and an automatic verdict:
`TOO_BROAD (>40%, staging-flood risk)` / `OK (15–40%, filtering noise)` /
`TOO_NARROW (<15%)`.

**Result on this repo (2026-06-14): `TOO_NARROW` — 10.0% pass (1/10).**
`skip no_cue 50%`, `skip type_gate 40%` (4 merge commits).

This number is **not a valid calibration of the cue set**, and is recorded as such:
the sample is pathological *by construction*. This repo has 10 commits — 4 GitHub
merge commits + 6 squashed per-phase mega-commits — not normal dev-cadence history.
The 15–40% healthy band assumes typical commit granularity, which does not exist here
yet. The single pass is a genuine decision (`chose … over … because`), so the filter
behaves correctly on real input; only the denominator is degenerate.

**What the calibration run actually delivered** — a real precision bug, caught by
inspecting *why* commits passed, not by a synthetic test:
- The first run showed 20% (2/10). One pass (`404f959`) matched **only** because the
  cue `chosen` was a substring of the code identifier `chosen_decision_id` in the
  commit body — not decision language. Substring cue matching was a precision leak
  that would misfire on identifiers like `chosen_`, `over_count`, `because_flag` and
  pollute memory with non-decisions.
- **Fixed:** cues are now matched as `\b`-anchored regex (`_CUE_RE`), longest-first.
  Regression-locked by a `test_day11` case (`chosen_decision_id` → `Skip(no_cue)`).
- After the fix, the false positive correctly drops; pass rate falls to 10% (1/10),
  the one remaining pass being a true decision.

### POST-MERGE TASK (documented, not deferred)

The calibration that the 15–40% band is meant to judge can only be measured on
real dev-cadence history — which **only accrues after the hook is installed and
running**. The pre-merge calibration is therefore impossible by construction, not by
omission.

> **TODO (post-merge):** once this repo (or any repo the hook is installed into) has
> accumulated ~50 normal-granularity commits, re-run
> `python -m hive.cli.capture calibrate 50` and record the verdict. If `TOO_BROAD`
> (>40%), the cue set is too permissive and risks staging/memory flooding — tighten
> `_DECISION_CUES`. If `OK` (15–40%), the floor is validated on real data. This is a
> required follow-up, owned by whoever first runs Hive against live history.

---

## Acceptance

- [ ] `extract.py` pure, every gate + skip-reason unit-tested.
- [ ] machine writes go through the real guard at confidence 0.6, tagged `git-hook`.
- [ ] sub-threshold commits dropped + audit-logged, never staged.
- [ ] hook install/uninstall idempotent, never clobbers an existing post-commit hook.
- [ ] `capture stats` surfaces cap-saturation + source + skip counts.
- [ ] days 1–10 green; retrieval benchmark unmoved.
