"""
Day 5 test: staging review workflow + auto-reject tuning.
Run from project root:  PYTHONIOENCODING=utf-8 python tests/test_day5.py

Spec (per hive_mind_project.md, Day 5):
    Staging review integrated into daily workflow.
    Auto-reject threshold tuned from staging data.

Method:
    1. Simulate review history:
         - 6 'Exact duplicate' staged records  → all rejected by reviewer
         - 6 "Missing 'why' field" staged records → 5 accepted, 1 rejected
    2. Run `tune_policies`. Expect:
         - 'Exact duplicate'      → action='auto_reject' (0% accept rate)
         - "Missing 'why' field"  → action='stage'       (~83% accept rate)
    3. Write a new exact-dup decision. Expect status='auto_rejected'
       (NOT staged), with no new row in `staging`.
    4. Write a new missing-why decision. Expect status='staged'.
    5. Stats endpoint returns both categories with correct counts.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hive.db.setup import init_db, get_connection
from hive import write_memory
from hive.core.policy import (
    category_of, tune_policies, stats, record_outcome, policy_action,
)


PROJECT = "day5-tune"


def sep(title):
    print(f"\n{'-'*60}")
    print(f"  {title}")
    print(f"{'-'*60}")


def staging_count(project):
    conn = get_connection()
    n = conn.execute(
        "SELECT COUNT(*) FROM staging WHERE project=?", (project,)
    ).fetchone()[0]
    conn.close()
    return n


def reset():
    init_db()
    conn = get_connection()
    for t in ("decisions", "snapshots", "open_tasks",
              "staging", "staging_history", "guard_policy"):
        conn.execute(f"DELETE FROM {t} WHERE project=?", (PROJECT,))
    conn.commit()
    conn.close()


def run():
    sep("Init + clean slate")
    reset()

    # ── 1. Simulate review history ───────────────────────────────────────
    sep("Simulating reviewer history")

    dup_reason  = "Exact duplicate: 'Using FastAPI over Flask for the API layer'"
    why_reason  = "Missing 'why' field — decisions without reasoning lose value over time"

    # 6 dup decisions, all rejected
    for _ in range(6):
        record_outcome(PROJECT, "decision", dup_reason, "rejected")

    # 6 missing-why decisions: 5 accepted, 1 rejected
    for _ in range(5):
        record_outcome(PROJECT, "decision", why_reason, "accepted")
    record_outcome(PROJECT, "decision", why_reason, "rejected")

    print(f"  6× {category_of(dup_reason)}  → all rejected")
    print(f"  6× {category_of(why_reason)}  → 5 accepted, 1 rejected")

    # ── 2. Tune ──────────────────────────────────────────────────────────
    sep("Running staging tune")
    summary = tune_policies(PROJECT)
    for r in summary:
        print(f"  {r['category']:<40} n={r['samples']}  "
              f"rate={r['accept_rate']*100:5.1f}%  → {r['action']}")

    assert policy_action(PROJECT, category_of(dup_reason)) == "auto_reject", \
        "exact-duplicate category should be auto_reject after 6/6 rejections"
    assert policy_action(PROJECT, category_of(why_reason)) == "stage", \
        "missing-why category should still be 'stage' at 83% accept rate"

    # ── 3. New exact-dup write → auto_rejected, not staged ───────────────
    sep("New exact-dup write should be auto_rejected (NOT staged)")

    # Seed a real decision so the dup actually triggers
    r0 = write_memory("decision", PROJECT, {
        "what":  "Using FastAPI over Flask for the API layer",
        "why":   "Async support and automatic OpenAPI docs out of the box",
        "agent": "claude-code",
    })
    assert r0["status"] == "committed", r0

    staged_before = staging_count(PROJECT)
    r_dup = write_memory("decision", PROJECT, {
        "what":  "Using FastAPI over Flask for the API layer",
        "why":   "Same reason",
        "agent": "claude-code",
    })
    staged_after = staging_count(PROJECT)
    print(f"  dup write → {r_dup['status']} | {r_dup['reason']}")
    print(f"  staging count: {staged_before} → {staged_after} (must not grow)")

    assert r_dup["status"] == "auto_rejected", \
        f"expected auto_rejected, got {r_dup['status']}"
    assert staged_after == staged_before, \
        "auto_rejected record must not land in staging"

    # ── 4. New missing-why write → still staged (policy unchanged) ───────
    sep("New missing-why write should still be staged")

    staged_before = staging_count(PROJECT)
    r_why = write_memory("decision", PROJECT, {
        "what":  "Redis chosen for distributed session caching",
        # no 'why'
        "agent": "claude-code",
    })
    staged_after = staging_count(PROJECT)
    print(f"  no-why write → {r_why['status']} | {r_why['reason']}")
    print(f"  staging count: {staged_before} → {staged_after}")

    assert r_why["status"] == "staged", \
        f"expected staged, got {r_why['status']}"
    assert staged_after == staged_before + 1

    # ── 5. Stats sanity ──────────────────────────────────────────────────
    sep("policy_stats() sanity")
    s = stats(PROJECT)
    cats = {row["category"]: row for row in s}

    dup_cat = category_of(dup_reason)
    why_cat = category_of(why_reason)

    assert dup_cat in cats, f"missing dup category in stats: {dup_cat}"
    assert why_cat in cats, f"missing why category in stats: {why_cat}"
    assert cats[dup_cat]["samples"]  == 6
    assert cats[dup_cat]["accepted"] == 0
    assert cats[dup_cat]["action"]   == "auto_reject"
    assert cats[why_cat]["samples"]  == 6
    assert cats[why_cat]["accepted"] == 5
    assert cats[why_cat]["action"]   == "stage"

    for cat, row in cats.items():
        print(f"  {cat:<40} n={row['samples']}  "
              f"acc={row['accepted']}  rej={row['rejected']}  → {row['action']}")

    sep("Day 5 complete — all assertions passed")


if __name__ == "__main__":
    run()
