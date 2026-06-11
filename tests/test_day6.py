"""
Day 6 soak harness — 7 simulated days of agent activity.
Run from project root:  PYTHONIOENCODING=utf-8 python tests/test_day6.py

Spec (per hive_mind_project.md, Week 2):
    Run Hive alongside real agent work for 7 days.
    Log every failure. Phase 1 milestone in next test.

Method:
    For each of 7 simulated days the harness:
      - writes a fixed mix of valid + intentionally bad decisions
      - asks the reader a fixed query battery (warm tier must produce a hit)
      - reviewer accepts/rejects last day's stagings
      - on day 3 we run `staging tune` so policy starts auto-rejecting
        the categories the reviewer has hammered

    Pass criteria:
      - audit_log captures EVERY write + every query (counts add up)
      - `staging tune` flips at least one category to auto_reject by day 4
      - by day 7, at least one auto_rejected event exists in audit
      - no decision is lost: committed + staged + auto_rejected = total writes
      - every query returned >=1 decision with positive top score
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hive.db.setup import init_db, get_connection
from hive import write_memory, read_memory
from hive.core.audit import counts, fails, tail
from hive.core.policy import tune_policies, stats as policy_stats


PROJECT = "soak-week2"

# Per-day write batch — same shape every day so the harness is repeatable.
def writes_for_day(day: int) -> list[tuple[str, dict]]:
    return [
        # 3 unique-ish valid decisions per day (varied by day index)
        ("decision", {
            "what":  f"Day {day}: adopt service mesh layer #{day} for traffic shaping",
            "why":   "Need uniform retry, circuit-break, and mTLS across services",
            "agent": "claude-code",
        }),
        ("decision", {
            "what":  f"Day {day}: pick caching tier #{day} for hot product reads",
            "why":   "Read amplification on the product page is killing the database",
            "agent": "claude-code",
        }),
        ("decision", {
            "what":  f"Day {day}: standardise structured logging format #{day}",
            "why":   "Free-form logs prevent meaningful aggregation in the lake",
            "agent": "claude-code",
        }),
        # 2 intentionally-bad writes per day → guaranteed staging traffic
        ("decision", {
            # Exact duplicate of day 1's first decision — staged after day 1
            "what":  "Day 1: adopt service mesh layer #1 for traffic shaping",
            "why":   "Same as before",
            "agent": "claude-code",
        }),
        ("decision", {
            # Vague — under 5 words
            "what":  "Use Vault",
            "why":   "Secrets",
            "agent": "claude-code",
        }),
        # 1 valid task per day
        ("open_task", {
            "description":    f"Wire telemetry exporter for new service mesh #{day}",
            "assigned_agent": "claude-code",
        }),
    ]


QUERIES = [
    "service mesh for traffic shaping",
    "caching tier choice for hot reads",
    "structured logging format decision",
]


def sep(title):
    print(f"\n{'-'*60}")
    print(f"  {title}")
    print(f"{'-'*60}")


def reset():
    init_db()
    conn = get_connection()
    for t in ("decisions", "snapshots", "open_tasks",
              "staging", "staging_history", "guard_policy", "audit_log"):
        conn.execute(f"DELETE FROM {t} WHERE project=?", (PROJECT,))
    conn.commit()
    conn.close()


def reviewer_pass(day: int):
    """
    Approximate a human reviewer.
    Day 1: skip (no precedent yet).
    Day 2+: reject everything staged with category 'Exact duplicate' or
            'Too vague'. Accept everything else.
    """
    from hive.core.writer import promote_from_staging, reject_from_staging
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, reason FROM staging WHERE project=?", (PROJECT,)
    ).fetchall()
    conn.close()

    accepted = rejected = 0
    for r in rows:
        if r["reason"].startswith(("Exact duplicate", "Too vague")):
            reject_from_staging(r["id"])
            rejected += 1
        else:
            promote_from_staging(r["id"])
            accepted += 1
    return accepted, rejected


def run():
    sep("Init + clean slate")
    reset()

    total_writes      = 0
    write_committed   = 0
    write_staged      = 0
    write_auto_rej    = 0
    queries_total     = 0
    auto_reject_flipped_at = None

    for day in range(1, 8):
        sep(f"Simulated day {day}")

        # ── Writes ────────────────────────────────────────────────
        batch = writes_for_day(day)
        for rtype, data in batch:
            r = write_memory(rtype, PROJECT, data)
            total_writes += 1
            if   r["status"] == "committed":     write_committed += 1
            elif r["status"] == "staged":        write_staged += 1
            elif r["status"] == "auto_rejected": write_auto_rej += 1

        # ── Queries ───────────────────────────────────────────────
        for q in QUERIES:
            mem = read_memory(PROJECT, q)
            queries_total += 1
            top = mem["warm"]["decisions"][0] if mem["warm"]["decisions"] else None
            if top is None or top["score"] <= 0:
                raise AssertionError(
                    f"day {day} query '{q}' returned no useful warm tier hit"
                )

        # ── Reviewer pass + (from day 3) tune ─────────────────────
        if day >= 2:
            acc, rej = reviewer_pass(day)
            print(f"  reviewer: accepted={acc}  rejected={rej}")

        if day >= 3:
            summary = tune_policies(PROJECT)
            for r in summary:
                if r["action"] == "auto_reject" and auto_reject_flipped_at is None:
                    auto_reject_flipped_at = day
                    print(f"  POLICY FLIP on day {day}: "
                          f"'{r['category']}' → auto_reject "
                          f"(n={r['samples']}, rate={r['accept_rate']*100:.0f}%)")

    # ── Final audit ──────────────────────────────────────────────────────
    sep("Final audit_log counts")
    c = counts(PROJECT)
    for kind in sorted(c, key=lambda k: -c[k]):
        print(f"  {kind:<22} {c[kind]:>5}")

    sep("Final policy snapshot")
    for r in policy_stats(PROJECT):
        print(f"  {r['category']:<42} n={r['samples']:>3} "
              f"rate={r['accept_rate']*100:5.1f}%  → {r['action']}")

    sep("Soak verdict")
    print(f"  total writes        : {total_writes}")
    print(f"  committed           : {write_committed}")
    print(f"  staged              : {write_staged}")
    print(f"  auto_rejected       : {write_auto_rej}")
    print(f"  queries             : {queries_total}")
    print(f"  policy flip day     : {auto_reject_flipped_at}")

    # Audit captured EVERY write + every query
    assert c.get("write_commit", 0)  == write_committed
    assert c.get("write_staged", 0)  == write_staged
    assert c.get("write_auto_rejected", 0) == write_auto_rej
    assert c.get("query", 0)         == queries_total

    # No write disappeared
    assert (write_committed + write_staged + write_auto_rej) == total_writes

    # Tune must have flipped at least one category by day 5 (MIN_SAMPLES=5
    # in policy.py — need 5 rejections of the same category first).
    assert auto_reject_flipped_at is not None and auto_reject_flipped_at <= 5, \
        f"expected an auto_reject policy by day 5, got {auto_reject_flipped_at}"

    # Audit must have at least one auto_rejected event recorded
    assert write_auto_rej >= 1, "expected ≥1 auto_rejected write across the week"

    sep("Day 6 soak passed")


if __name__ == "__main__":
    run()
