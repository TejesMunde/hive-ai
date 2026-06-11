"""
Day 2 end-to-end test.
Run from /hive_mind:  python tests/test_day2.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hive.db.setup import init_db, get_connection
from hive import write_memory, read_memory
from hive.core.writer import close_task, promote_from_staging, reject_from_staging


def sep(title):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


def count_staging(project):
    conn = get_connection()
    n = conn.execute(
        "SELECT COUNT(*) FROM staging WHERE project=?", (project,)
    ).fetchone()[0]
    conn.close()
    return n


def run():
    sep("Init")
    init_db()

    # Wipe project data for a clean test run
    conn = get_connection()
    conn.execute("DELETE FROM decisions WHERE project='day2-test'")
    conn.execute("DELETE FROM open_tasks WHERE project='day2-test'")
    conn.execute("DELETE FROM snapshots WHERE project='day2-test'")
    conn.execute("DELETE FROM staging   WHERE project='day2-test'")
    conn.commit()
    conn.close()
    print("  Clean slate for project 'day2-test'")

    project = "day2-test"

    # ── Valid baseline decisions ─────────────────────────────────────────────
    sep("Writing valid baseline decisions")

    r1 = write_memory("decision", project, {
        "what":  "Using FastAPI over Flask for the API layer",
        "why":   "FastAPI gives us async support and automatic OpenAPI docs out of the box",
        "agent": "claude-code",
    })
    print(f"  FastAPI decision    → {r1['status']}")
    assert r1["status"] == "committed", f"Expected committed, got {r1}"

    r2 = write_memory("decision", project, {
        "what":  "PostgreSQL chosen as the primary database over SQLite",
        "why":   "Production workload needs concurrent writes and row-level locking",
        "agent": "claude-code",
    })
    print(f"  PostgreSQL decision → {r2['status']}")
    assert r2["status"] == "committed"

    r3 = write_memory("open_task", project, {
        "description":    "Implement JWT authentication middleware for all protected routes",
        "assigned_agent": "claude-code",
    })
    print(f"  JWT task            → {r3['status']}")
    assert r3["status"] == "committed"

    # ── Guard rule: missing 'why' ────────────────────────────────────────────
    sep("Guard rule: missing 'why' goes to staging")

    r4 = write_memory("decision", project, {
        "what":  "Redis chosen for session caching layer across services",
        "agent": "claude-code",
        # deliberately no 'why'
    })
    print(f"  No-why decision → {r4['status']} | {r4['reason']}")
    assert r4["status"] == "staged"
    assert count_staging(project) == 1
    staged_id = None
    conn = get_connection()
    staged_id = conn.execute(
        "SELECT id FROM staging WHERE project=?", (project,)
    ).fetchone()["id"]
    conn.close()

    # ── Guard rule: too vague ────────────────────────────────────────────────
    sep("Guard rule: vague entries go to staging")

    r5 = write_memory("decision", project, {
        "what":  "Use Redis",   # under 5 words
        "why":   "Fast",
        "agent": "claude-code",
    })
    print(f"  Vague decision  → {r5['status']} | {r5['reason']}")
    assert r5["status"] == "staged"

    # ── Guard rule: exact duplicate ──────────────────────────────────────────
    sep("Guard rule: exact duplicate")

    r6 = write_memory("decision", project, {
        "what":  "Using FastAPI over Flask for the API layer",   # identical to r1
        "why":   "Same reason as before",
        "agent": "claude-code",
    })
    print(f"  Exact dup       → {r6['status']} | {r6['reason']}")
    assert r6["status"] == "staged"

    # ── Guard rule: fuzzy duplicate ──────────────────────────────────────────
    sep("Guard rule: fuzzy duplicate (rewording of existing decision)")

    r7 = write_memory("decision", project, {
        "what":  "We are using FastAPI instead of Flask for our API",  # ~90% similar
        "why":   "Same performance reasons",
        "agent": "claude-sonnet",
    })
    print(f"  Fuzzy dup       → {r7['status']} | {r7['reason']}")
    assert r7["status"] == "staged"

    # ── Guard rule: contradiction ────────────────────────────────────────────
    sep("Guard rule: contradiction")

    r8 = write_memory("decision", project, {
        "what":  "Using SQLite over PostgreSQL for the primary database",  # contradicts r2
        "why":   "Simpler for local dev",
        "agent": "claude-code",
    })
    print(f"  Contradiction   → {r8['status']} | {r8['reason']}")
    assert r8["status"] == "staged"

    # ── Staging review: accept ───────────────────────────────────────────────
    sep("Staging review: accept the missing-why record")

    n_before = count_staging(project)
    result   = promote_from_staging(staged_id)
    n_after  = count_staging(project)

    print(f"  Promote result  → {result['status']}")
    print(f"  Staging count   → {n_before} before, {n_after} after")
    assert result["status"] == "promoted"
    assert n_after == n_before - 1

    # ── Staging review: reject ───────────────────────────────────────────────
    sep("Staging review: reject the vague record")

    conn = get_connection()
    vague_id = conn.execute(
        "SELECT id FROM staging WHERE project=? AND reason LIKE '%vague%' LIMIT 1",
        (project,)
    ).fetchone()
    conn.close()

    if vague_id:
        result2 = reject_from_staging(vague_id["id"])
        print(f"  Reject result   → {result2['status']}")
        assert result2["status"] == "rejected"
    else:
        print("  (no vague record found to reject — OK)")

    # ── close_task ───────────────────────────────────────────────────────────
    sep("close_task()")

    task_id   = r3["id"]
    close_res = close_task(task_id)
    print(f"  Close task      → {close_res['status']}")
    assert close_res["status"] == "closed"

    conn = get_connection()
    status = conn.execute(
        "SELECT status FROM open_tasks WHERE id=?", (task_id,)
    ).fetchone()["status"]
    conn.close()
    print(f"  DB status after → {status}")
    assert status == "done"

    # Closing already-closed task should return not_found
    close_res2 = close_task(task_id)
    print(f"  Re-close        → {close_res2['status']} (expected: not_found)")
    assert close_res2["status"] == "not_found"

    # ── read_memory after all writes ─────────────────────────────────────────
    sep("read_memory — final state check")

    mem = read_memory(project, "how is the API framework chosen")
    print(f"  Token estimate  → {mem['token_estimate']}")
    print(f"  Decisions       → {len(mem['warm']['decisions'])}")
    print(f"  Open tasks      → {len(mem['hot']['open_tasks'])}")

    for d in mem["warm"]["decisions"]:
        print(f"    [{d['confidence']:.1f}] {d['what'][:65]}")

    assert len(mem["warm"]["decisions"]) >= 2
    # JWT task should be closed, so 0 open tasks
    assert len(mem["hot"]["open_tasks"]) == 0

    sep("Day 2 complete — all assertions passed ✓")


if __name__ == "__main__":
    run()
