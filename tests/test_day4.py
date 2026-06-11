"""
Day 4 real-project retrieval benchmark.
Run from project root:  PYTHONIOENCODING=utf-8 python tests/test_day4.py

Spec (per hive_mind_project.md, Day 4):
    Feed 10 real decisions from real project.
    Run 5 real agent queries.
    Measure retrieval accuracy.
    Fix everything that breaks.

Method:
    Seed the 10 actual technical decisions documented in
    hive_mind_project.md (Technical Decisions Made + Architecture).
    Run 5 plausible agent questions a junior dev or new agent would ask
    when they open the repo cold.
    Pass: top-1 hit rate >= 4/5  AND  MRR@5 >= 0.9.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hive.db.setup import init_db, get_connection
from hive import write_memory, read_memory


PROJECT = "hive-mind-real"

# Pulled verbatim from hive_mind_project.md decision list + architecture notes.
DECISIONS = [
    ("SQLite chosen over PostgreSQL for Phase 1 persistence",
     "Zero setup, file-based, inspectable, migratable later. Right tool for Phase 1, wrong for Phase 6."),

    ("Jaccard token overlap selected over SequenceMatcher for duplicate detection",
     "SequenceMatcher scores character similarity. Jaccard scores concept overlap, catching semantic rewordings correctly."),

    ("Staging bad records instead of rejecting them outright",
     "Deleted bad data gives no signal. Staged bad data teaches what the system is getting wrong. Each staged record is feedback."),

    ("Flat keyword index built from day one even before semantic search exists",
     "Phase 2 needs an index for embedding retrieval. Building the retrieval layer now means swapping cosine in without a rewrite."),

    ("Zero external dependencies allowed in Phase 1",
     "Pure stdlib means zero install friction. Embeddings and FastAPI are deferred until they earn their seat."),

    ("Write guard runs before every commit to memory",
     "One corrupt record poisons every future agent call. The guard is non-negotiable, not optional infrastructure."),

    ("Hot warm cold tier system for memory retrieval budgets",
     "Hot is current tasks plus latest snapshot. Warm is ranked decisions. Cold is full history. Tiers keep token costs predictable."),

    ("Three deployment modes selectable at hive init: local, team, cloud",
     "Solo devs run local SQLite. Teams run shared Postgres server. Commercial users get hosted SaaS with dashboard."),

    ("Migration between deployment paths is always a single hive migrate command",
     "Users start local then graduate to team or cloud without manual export. No reconfiguration step."),

    ("Decision provenance and dead ends stored in cold tier from Phase 3",
     "Agents must not re-suggest already-rejected approaches. Every decision links to what it replaced."),
]

# 5 plausible cold-open agent queries.
# expected_substr is matched lowercase against top-1 `what`.
QUERIES = [
    ("which database does phase 1 use",                            "sqlite"),
    ("how does the guard detect duplicate decisions",              "jaccard"),
    ("what happens to records that fail validation",               "staging"),
    ("do we need to install any third party packages",             "external"),
    ("how is the memory budget split across tiers",                "tier"),
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
            "agent": "doc-import",
        })
        assert r["status"] == "committed", (
            f"seed failed for: {what[:50]} | {r['reason']}"
        )


def run():
    sep("Init + seed 10 real decisions from hive_mind_project.md")
    seed()
    print(f"  {len(DECISIONS)} decisions seeded under '{PROJECT}'")

    sep("Benchmark — 5 real cold-open queries")

    hits = 0
    mrr  = 0.0
    K    = 5
    misses = []

    for query, expected in QUERIES:
        mem  = read_memory(PROJECT, query)
        decs = mem["warm"]["decisions"][:K]

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
        top  = decs[0]["what"] if decs else "(empty)"
        score = decs[0]["score"] if decs else 0.0
        print(f"  [{flag}] q='{query}'")
        print(f"         expected '{expected}'  rank={rank or 'none'}  rr={rr:.2f}")
        print(f"         top-1 → [{score:.3f}] {top}")
        if not ok:
            misses.append((query, expected, [d["what"][:60] for d in decs[:3]]))

    mrr /= len(QUERIES)

    sep("Result")
    print(f"  top-1 hit rate : {hits}/{len(QUERIES)}")
    print(f"  MRR@{K}         : {mrr:.3f}")

    if misses:
        sep("Misses (for analysis)")
        for q, exp, tops in misses:
            print(f"  query    : {q}")
            print(f"  expected : {exp}")
            for i, t in enumerate(tops, 1):
                print(f"    top-{i} : {t}")

    assert hits >= 4,  f"top-1 hit rate too low: {hits}/5"
    assert mrr  >= 0.9, f"MRR too low: {mrr:.3f}"

    sep("Day 4 benchmark passed")


if __name__ == "__main__":
    run()
