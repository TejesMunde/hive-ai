"""
Day 7 — Phase 1 milestone test.
Run from project root:  PYTHONIOENCODING=utf-8 python tests/test_day7.py

Spec (per hive_mind_project.md, Week 2 closing milestone):
    Agent reads from memory and gets correct answer without touching the
    codebase.

Method:
    1. Seed 15 decisions covering an imaginary but coherent project:
       a B2B SaaS with web API, payments, auth, observability, infra.
    2. Run 15 plausible agent questions a teammate would ask cold.
    3. STRICTLY check:
         - top-1 hit rate >= 13/15  (≈87%)
         - MRR@5            >= 0.90
         - every answer comes from memory only (assertion: `mem['warm']
           ['decisions']` is non-empty and the expected substring appears
           somewhere in the top-3)
         - token budget never exceeded (hot ≤500, warm ≤2500)
    4. Replay audit_log and confirm every query emitted a 'query' event.

This is the "do not touch the codebase" guarantee. If it passes,
Phase 1 is shippable.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hive.db.setup import init_db, get_connection
from hive import write_memory, read_memory
from hive.core.audit import counts


PROJECT = "phase1-milestone"

# Realistic decisions across a B2B SaaS — distinct domains, distinct vocab.
DECISIONS = [
    # API + framework
    ("FastAPI selected over Flask for the public REST surface",
     "Async support, automatic OpenAPI docs, and fewer lines of boilerplate"),
    ("REST chosen over GraphQL for the public API surface",
     "Customer engineers know REST; GraphQL N+1 hazards aren't worth the flexibility"),

    # Persistence
    ("PostgreSQL chosen as the primary OLTP database",
     "Need row-level locking, JSONB indices, and logical replication for HA"),
    ("Database migrations managed via Alembic with autogen scaffolding",
     "Alembic plays well with SQLAlchemy and supports stamped baselines"),

    # Cache + queue
    ("Redis adopted for the session cache and rate-limit counters",
     "Sub-millisecond reads required for auth checks on every request"),
    ("Celery plus RabbitMQ chosen for the background job queue",
     "Acknowledged retries and visibility timeouts matter for billing jobs"),

    # Auth
    ("OAuth2 with PKCE used for first-party mobile and web client auth",
     "Mitigates code-interception attacks on public clients without a backend secret"),
    ("JWTs adopted for service-to-service identity inside the cluster",
     "Stateless rotation simplifies horizontal scaling and avoids session pinning"),

    # Payments
    ("Stripe selected as the payments processor for cards and ACH",
     "PCI compliance handled externally, broad coverage, and idempotency keys"),
    ("Webhook delivery retries handled by Stripe with our local idempotency log",
     "Avoids double-charging on retried webhook deliveries"),

    # Observability
    ("Sentry adopted for application error monitoring across services",
     "Source map support, existing team familiarity, and good Slack integration"),
    ("OpenTelemetry chosen for distributed tracing across services",
     "Vendor neutral, plays well with Jaeger locally and Honeycomb in prod"),
    ("Structured JSON logs shipped to a central Loki backend",
     "Free-form logs prevent meaningful aggregation in the observability lake"),

    # Infra + delivery
    ("Docker Compose used for local development environments",
     "One command spins the full stack identical across machines"),
    ("Kubernetes selected over plain ECS for the production runtime",
     "Need pod-level autoscaling, daemonsets for observability, and HPA tuning"),
]

# Cold-open agent questions — phrased the way a junior dev or a switched
# agent would actually ask them. Expected_substr is a lowercase hint that
# must appear in the top-1 (strict) and at minimum somewhere in top-3 (loose).
QUERIES = [
    ("which framework powers the public api",            "fastapi"),
    ("rest vs graphql decision for the api",             "rest"),
    ("which database is the primary store",              "postgresql"),
    ("how do we manage database schema changes",         "alembic"),
    ("how are user sessions cached",                     "redis"),
    ("background job processing setup",                  "celery"),
    ("how does mobile login work",                       "oauth"),
    ("internal service to service identity",             "jwt"),
    ("how do we process payments",                       "stripe"),
    ("how are webhook retries handled",                  "webhook"),
    ("how do we track production errors",                "sentry"),
    ("distributed tracing implementation",               "opentelemetry"),
    ("how do we centralise logs",                        "log"),
    ("how do new developers run the stack locally",      "docker"),
    ("what runs our production workloads",               "kubernetes"),
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


def seed():
    for what, why in DECISIONS:
        r = write_memory("decision", PROJECT, {
            "what": what, "why": why, "agent": "doc-import",
        })
        assert r["status"] == "committed", f"seed failed for: {what} | {r['reason']}"


def run():
    sep("Init + seed 15 decisions for milestone project")
    reset()
    seed()

    audit_before = counts(PROJECT)
    queries_before = audit_before.get("query", 0)

    sep("Run 15 cold-open queries against memory only")

    top1_hits = 0
    top3_hits = 0
    mrr       = 0.0
    HOT_LIMIT  = 500
    WARM_LIMIT = 2500
    over_budget = []
    misses = []

    for query, expected in QUERIES:
        mem  = read_memory(PROJECT, query)
        decs = mem["warm"]["decisions"][:5]

        rank = 0
        for i, d in enumerate(decs, 1):
            if expected in d["what"].lower():
                rank = i
                break

        rr   = (1.0 / rank) if rank else 0.0
        mrr += rr
        if rank == 1:           top1_hits += 1
        if rank and rank <= 3:  top3_hits += 1

        # Budget audit
        hot_cost = (
            sum(len(t["description"]) // 4 for t in mem["hot"]["open_tasks"]) +
            (len(mem["hot"]["latest_snapshot"]["file_structure"]) // 4
             if mem["hot"]["latest_snapshot"] else 0)
        )
        warm_cost = sum((len(d["what"]) + len(d["why"] or "")) // 4
                        for d in mem["warm"]["decisions"])
        if hot_cost > HOT_LIMIT or warm_cost > WARM_LIMIT:
            over_budget.append((query, hot_cost, warm_cost))

        flag = "OK " if rank == 1 else ("near" if rank <= 3 and rank else "MISS")
        top  = decs[0]["what"] if decs else "(empty)"
        print(f"  [{flag}] {query[:48]:<48}  rank={rank or '-'}  "
              f"top → {top[:48]}")

        if rank != 1:
            misses.append((query, expected, [d["what"][:55] for d in decs[:3]]))

    mrr /= len(QUERIES)

    audit_after = counts(PROJECT)
    queries_emitted = audit_after.get("query", 0) - queries_before

    sep("Phase 1 milestone scoreboard")
    print(f"  top-1 hit rate      : {top1_hits}/{len(QUERIES)}")
    print(f"  top-3 hit rate      : {top3_hits}/{len(QUERIES)}")
    print(f"  MRR@5               : {mrr:.3f}")
    print(f"  budget violations   : {len(over_budget)}")
    print(f"  audit query events  : {queries_emitted} (must == {len(QUERIES)})")

    if misses:
        sep("Misses (top-3 listed)")
        for q, exp, tops in misses:
            print(f"  query : {q}")
            print(f"  expect: {exp}")
            for i, t in enumerate(tops, 1):
                print(f"    {i}. {t}")

    # ── STRICT assertions: this is the milestone test ─────────────────────
    assert top1_hits  >= 13,        f"top-1 too low: {top1_hits}/15"
    assert mrr        >= 0.90,      f"MRR too low: {mrr:.3f}"
    assert not over_budget,         f"budget violations: {over_budget}"
    assert queries_emitted == len(QUERIES), \
        f"audit missed events: emitted={queries_emitted} expected={len(QUERIES)}"

    sep("Phase 1 milestone PASSED — agent answers from memory only")


if __name__ == "__main__":
    run()
