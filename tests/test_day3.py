"""
Day 3 retrieval accuracy benchmark.
Run from project root:  PYTHONIOENCODING=utf-8 python tests/test_day3.py

Spec (per hive_mind_project.md, Day 3):
    `read_memory()` accuracy tightened. Keyword ranking improved.
    Real accuracy benchmark against actual project data.

Method:
    Seed 10 realistic decisions across disjoint domains.
    Run 5 queries that each have one obviously correct top-1 answer.
    Pass if top-1 hit-rate >= 4/5 and overall MRR@5 >= 0.8.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hive.db.setup import init_db, get_connection
from hive import write_memory, read_memory


PROJECT = "day3-bench"

DECISIONS = [
    ("PostgreSQL chosen as the primary database",
     "Needs concurrent writes and row-level locking for the order pipeline"),
    ("Redis selected for the session cache layer",
     "Sub-millisecond reads required for auth checks on every request"),
    ("FastAPI adopted for the public REST surface",
     "Async support and automatic OpenAPI documentation out of the box"),
    ("Stripe integrated for payment processing",
     "PCI compliance handled externally and global card coverage"),
    ("Celery + RabbitMQ for background job queue",
     "Reliable retry semantics and visibility into long-running tasks"),
    ("Sentry chosen for error monitoring",
     "Source map support and existing team familiarity"),
    ("Docker Compose for local development environments",
     "One command spins the full stack identical across machines"),
    ("Pytest as the canonical test runner",
     "Fixture system and rich plugin ecosystem beats unittest"),
    ("OpenTelemetry for distributed tracing across services",
     "Vendor neutral and works with Jaeger and Honeycomb backends"),
    ("React with Vite for the admin dashboard frontend",
     "Fast HMR and modern ESM build pipeline"),
]

# query  →  expected substring (case-insensitive) in top-1 `what`
QUERIES = [
    ("which database does the order pipeline use",       "postgresql"),
    ("how do we cache user sessions for fast auth",      "redis"),
    ("background job queue for retries",                 "celery"),
    ("which framework powers the public api",            "fastapi"),
    ("how do we track production errors",                "sentry"),
]


def sep(title):
    print(f"\n{'-'*60}")
    print(f"  {title}")
    print(f"{'-'*60}")


def seed():
    init_db()
    conn = get_connection()
    for table in ("decisions", "snapshots", "open_tasks", "staging"):
        conn.execute(f"DELETE FROM {table} WHERE project=?", (PROJECT,))
    conn.commit()
    conn.close()

    for what, why in DECISIONS:
        r = write_memory("decision", PROJECT, {
            "what":  what,
            "why":   why,
            "agent": "claude-code",
        })
        assert r["status"] == "committed", f"seed failed for: {what} | {r['reason']}"


def run():
    sep("Init + seed 10 decisions")
    seed()
    print(f"  {len(DECISIONS)} decisions seeded under '{PROJECT}'")

    sep("Benchmark — 5 queries")

    hits   = 0
    mrr    = 0.0
    K      = 5

    for query, expected in QUERIES:
        mem  = read_memory(PROJECT, query)
        decs = mem["warm"]["decisions"][:K]
        top  = decs[0]["what"] if decs else "(empty)"

        # rank of first match
        rank = 0
        for i, d in enumerate(decs, 1):
            if expected in d["what"].lower():
                rank = i
                break

        rr   = (1.0 / rank) if rank else 0.0
        mrr += rr
        ok   = rank == 1
        hits += int(ok)

        flag = "OK " if ok else "MISS"
        print(f"  [{flag}] q='{query}'")
        print(f"         expected match: '{expected}'  hit@rank={rank or 'none'}  rr={rr:.2f}")
        print(f"         top-1 → [{decs[0]['score']:.3f}] {top}")

    mrr /= len(QUERIES)

    sep("Result")
    print(f"  top-1 hit rate : {hits}/{len(QUERIES)}")
    print(f"  MRR@{K}         : {mrr:.3f}")

    assert hits >= 4, f"top-1 hit rate too low: {hits}/5"
    assert mrr  >= 0.8, f"MRR too low: {mrr:.3f}"

    sep("Day 3 benchmark passed")


if __name__ == "__main__":
    run()
