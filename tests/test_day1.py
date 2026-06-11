"""
Day 1 end-to-end test.
Run from project root:  PYTHONIOENCODING=utf-8 python tests/test_day1.py

Spec (per hive_mind_project.md, Day 1):
    3 decisions + 1 snapshot + 2 tasks committed,
    3 bad records caught (staged),
    query returns correct token-budgeted context.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hive.db.setup import init_db, get_connection
from hive import write_memory, read_memory


def sep(title):
    print(f"\n{'-'*60}")
    print(f"  {title}")
    print(f"{'-'*60}")


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

    project = "day1-test"

    # Clean slate
    conn = get_connection()
    for table in ("decisions", "snapshots", "open_tasks", "staging"):
        conn.execute(f"DELETE FROM {table} WHERE project=?", (project,))
    conn.commit()
    conn.close()
    print(f"  Clean slate for project '{project}'")

    # ── 3 valid decisions ────────────────────────────────────────────────
    sep("Committing 3 decisions")

    d1 = write_memory("decision", project, {
        "what":  "Using FastAPI over Flask for the API layer",
        "why":   "Async support and automatic OpenAPI docs out of the box",
        "agent": "claude-code",
    })
    d2 = write_memory("decision", project, {
        "what":  "PostgreSQL chosen as the primary database",
        "why":   "Concurrent writes and row-level locking required at scale",
        "agent": "claude-code",
    })
    d3 = write_memory("decision", project, {
        "what":  "JWT tokens for stateless auth across microservices",
        "why":   "Avoids server-side session store and scales horizontally",
        "agent": "claude-code",
    })
    for r in (d1, d2, d3):
        print(f"  decision → {r['status']}")
        assert r["status"] == "committed", f"Expected committed, got {r}"

    # ── 1 snapshot ───────────────────────────────────────────────────────
    sep("Committing 1 snapshot")

    s1 = write_memory("snapshot", project, {
        "file_structure": "app/main.py, app/api/, app/db/, app/auth/, tests/",
        "active_stack":   "FastAPI, PostgreSQL, JWT, pytest",
        "current_module": "app/auth/",
    })
    print(f"  snapshot → {s1['status']}")
    assert s1["status"] == "committed"

    # ── 2 open tasks ─────────────────────────────────────────────────────
    sep("Committing 2 open tasks")

    t1 = write_memory("open_task", project, {
        "description":    "Implement JWT refresh token rotation endpoint",
        "assigned_agent": "claude-code",
    })
    t2 = write_memory("open_task", project, {
        "description":    "Wire PostgreSQL connection pool with asyncpg",
        "assigned_agent": "claude-code",
    })
    for r in (t1, t2):
        print(f"  open_task → {r['status']}")
        assert r["status"] == "committed"

    # ── 3 bad records (must be staged, not committed) ────────────────────
    sep("Guard should catch 3 bad records")

    staged_before = count_staging(project)

    # bad #1: missing required field 'what'
    b1 = write_memory("decision", project, {
        "what":  "",
        "why":   "Nothing to decide",
        "agent": "claude-code",
    })
    print(f"  missing 'what' → {b1['status']} | {b1['reason']}")
    assert b1["status"] == "staged"

    # bad #2: too vague (under 5 words)
    b2 = write_memory("decision", project, {
        "what":  "Use Redis",
        "why":   "Caching",
        "agent": "claude-code",
    })
    print(f"  vague          → {b2['status']} | {b2['reason']}")
    assert b2["status"] == "staged"

    # bad #3: exact duplicate of d1
    b3 = write_memory("decision", project, {
        "what":  "Using FastAPI over Flask for the API layer",
        "why":   "Same reason again",
        "agent": "claude-code",
    })
    print(f"  exact dup      → {b3['status']} | {b3['reason']}")
    assert b3["status"] == "staged"

    staged_after = count_staging(project)
    print(f"  staging count  → {staged_before} -> {staged_after} (delta = 3)")
    assert staged_after - staged_before == 3

    # ── Query returns correct context ────────────────────────────────────
    sep("read_memory — query returns correct slice")

    mem = read_memory(project, "primary database storage choice")

    print(f"  token_estimate → {mem['token_estimate']}")
    print(f"  decisions      → {len(mem['warm']['decisions'])}")
    print(f"  open_tasks     → {len(mem['hot']['open_tasks'])}")
    print(f"  snapshot?      → {mem['hot']['latest_snapshot'] is not None}")

    assert len(mem["warm"]["decisions"]) == 3
    assert len(mem["hot"]["open_tasks"]) == 2
    assert mem["hot"]["latest_snapshot"] is not None
    assert mem["token_estimate"] > 0

    # Top-ranked decision for "which database" should be the PostgreSQL one
    top = mem["warm"]["decisions"][0]
    print(f"  top decision   → [{top['score']}] {top['what']}")
    assert "postgresql" in top["what"].lower(), (
        f"Expected DB decision on top, got: {top['what']}"
    )

    sep("Day 1 complete — all assertions passed")


if __name__ == "__main__":
    run()
