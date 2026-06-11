"""
Synthetic eval corpus for recall benchmark.

100 decisions across 10 domains. Each decision has 1-2 paired queries with
controlled paraphrase. Categories:

  exact      : query shares vocabulary with decision (TF-IDF should win)
  paraphrase : query semantically equivalent, lexically disjoint
  vocab_gap  : query uses synonyms / hypernyms only
  negation   : query asks "why NOT X" — wrong if returns X
  adversarial: query close to a decoy decision; correct answer is harder

Output JSON:
  decisions: [{id, what, why}]
  queries:   [{query, expected_id, category}]

Seed deterministic. Re-runnable.
"""

import json
import os
import random

SEED = 42
OUT_PATH = os.path.join(os.path.dirname(__file__), "eval_corpus.json")

DOMAINS = [
    {
        "domain": "database",
        "decisions": [
            ("Chose PostgreSQL for primary OLTP store",
             "Strong ACID guarantees and JSONB support fit our mixed relational + document workload",
             ["why did we pick our database",
              "what powers persistent storage",
              "reasoning behind the data store choice"]),
            ("Rejected MongoDB for primary store",
             "Lack of multi-document transactions caused inventory desync in 2024 pilot",
             ["why we did not pick MongoDB",
              "issues with document database for our use case"]),
            ("Selected Redis for session cache",
             "Sub-millisecond reads required for auth token validation under 10k rps",
             ["where do session tokens live",
              "what handles ephemeral key-value lookups"]),
            ("DuckDB used for analytical exports",
             "In-process OLAP avoids spinning up a warehouse for monthly reports",
             ["how do we run analytical queries",
              "embedded analytics engine choice"]),
            ("SQLite chosen for local dev fixtures",
             "Zero-config single-file database accelerates contributor onboarding",
             ["what database powers local development",
              "lightweight dev fixture store"]),
        ],
    },
    {
        "domain": "framework",
        "decisions": [
            ("FastAPI selected for HTTP layer",
             "Native async, OpenAPI generation, and pydantic validation cut boilerplate vs Flask",
             ["why we chose our API framework",
              "what serves HTTP requests"]),
            ("Rejected Django for this service",
             "Monolithic ORM and admin coupling fights our event-driven design",
             ["why Django was not chosen",
              "reasoning against full-stack web framework"]),
            ("Starlette internals exposed for streaming endpoints",
             "FastAPI's StreamingResponse insufficient for backpressure-aware SSE",
             ["why drop down below the framework for SSE",
              "streaming endpoint implementation choice"]),
        ],
    },
    {
        "domain": "auth",
        "decisions": [
            ("JWT chosen for stateless auth across microservices",
             "Avoids central session lookup; signature verify at edge scales horizontally",
             ["why stateless tokens",
              "how does cross-service authentication work"]),
            ("Refresh tokens stored in HttpOnly cookies",
             "Mitigates XSS exfiltration vs localStorage; CSRF guarded by SameSite=Lax",
             ["where do refresh tokens live in the browser",
              "client-side token storage choice"]),
            ("Argon2id picked over bcrypt for password hashing",
             "Memory-hard parameters resist GPU brute force in 2025 threat models",
             ["why this password hash algorithm",
              "reasoning behind KDF choice"]),
            ("OAuth2 PKCE flow enforced for native clients",
             "Authorization code interception attacks on public clients require PKCE",
             ["mobile and desktop auth flow choice",
              "native client OAuth handling"]),
        ],
    },
    {
        "domain": "deployment",
        "decisions": [
            ("Kubernetes selected for production orchestration",
             "Multi-tenant workloads need pod-level isolation and rolling deploys",
             ["why container orchestration",
              "production runtime platform reasoning"]),
            ("Rejected serverless for hot path",
             "Cold-start P99 exceeded 800ms — unacceptable for synchronous API",
             ["why not lambda for the API",
              "serverless versus container decision"]),
            ("Helm charts source of truth, not raw YAML",
             "Templating prevents drift across staging/prod and parameterizes secrets",
             ["how do we manage k8s manifests",
              "kubernetes config templating choice"]),
            ("ArgoCD chosen for GitOps deploy",
             "Declarative reconciliation surfaces drift; PR-driven prod changes",
             ["what controls cluster sync",
              "GitOps tool selection"]),
        ],
    },
    {
        "domain": "observability",
        "decisions": [
            ("OpenTelemetry adopted for distributed tracing",
             "Vendor-neutral instrumentation avoids re-wiring on backend swaps",
             ["why this tracing standard",
              "distributed tracing instrumentation choice"]),
            ("Loki picked over Elasticsearch for logs",
             "10x cheaper storage for our log volume; label-based queries suffice",
             ["log aggregation backend reasoning",
              "why not Elasticsearch for logs"]),
            ("Prometheus + Mimir for metrics at scale",
             "Long-term storage with PromQL compatibility; sharded for horizontal scale",
             ["metrics storage architecture",
              "how do we keep metrics history"]),
            ("Sentry captures application errors",
             "Source-mapped stack traces and release tagging beat raw log search",
             ["error tracking tool choice",
              "exception aggregation reasoning"]),
        ],
    },
    {
        "domain": "messaging",
        "decisions": [
            ("Kafka chosen for event backbone",
             "Replay semantics and partition ordering required for billing audit trail",
             ["why event streaming platform",
              "durable message log choice"]),
            ("Rejected RabbitMQ for primary bus",
             "Lacks log replay; consumer-acknowledge model loses messages on consumer crashes",
             ["why not RabbitMQ",
              "AMQP versus log-based messaging"]),
            ("NATS selected for control-plane RPC",
             "Sub-millisecond pub/sub for ephemeral coordination signals",
             ["lightweight messaging for control plane",
              "internal RPC fabric reasoning"]),
        ],
    },
    {
        "domain": "testing",
        "decisions": [
            ("Pytest as primary test runner",
             "Fixture composition and parametrization reduce duplication vs unittest",
             ["test framework choice",
              "why we use this Python test runner"]),
            ("Integration tests hit real Postgres in CI",
             "Mocked tests masked migration failure in Q3 2024 incident",
             ["why no mocked database in tests",
              "integration test database policy"]),
            ("Property-based tests via Hypothesis for parsers",
             "Random input generation found 11 edge cases unit tests missed",
             ["how we test parser correctness",
              "fuzzing approach for input handlers"]),
            ("E2E tests run nightly, not per-PR",
             "30-min runtime makes per-PR enforcement a merge bottleneck",
             ["why end-to-end tests are not on every PR",
              "browser test cadence reasoning"]),
        ],
    },
    {
        "domain": "frontend",
        "decisions": [
            ("React Server Components for marketing pages",
             "Zero client JS on landing pages improves LCP from 2.1s to 0.6s",
             ["why server components for static pages",
              "SSR strategy for landing pages"]),
            ("Tailwind picked over CSS-in-JS",
             "Atomic classes ship less CSS and avoid runtime style serialization",
             ["styling approach reasoning",
              "why not styled-components"]),
            ("Tanstack Query handles server state",
             "Cache invalidation and refetch policies cleaner than Redux for our shape",
             ["client data fetching library choice",
              "server state management reasoning"]),
        ],
    },
    {
        "domain": "data_pipeline",
        "decisions": [
            ("Airflow chosen for batch orchestration",
             "DAG-as-code and rich operator ecosystem fit our ETL surface",
             ["batch scheduler choice",
              "workflow orchestration tool reasoning"]),
            ("dbt models source of truth for transforms",
             "SQL-first transformations with lineage docs beat hand-rolled scripts",
             ["how analytics transformations are managed",
              "SQL transformation framework choice"]),
            ("Iceberg adopted as table format",
             "Schema evolution and time-travel queries unblock GDPR delete-by-id at scale",
             ["lakehouse table format reasoning",
              "why this open table format"]),
        ],
    },
    {
        "domain": "ml",
        "decisions": [
            ("ONNX runtime for production inference",
             "Decouples training framework from serving; CPU SIMD kernels match GPU latency at our batch size",
             ["model serving runtime choice",
              "why this inference engine"]),
            ("HNSW index for vector recall",
             "Logarithmic query time at our 500k corpus; recall@10 above 0.95 with M=16",
             ["vector search algorithm choice",
              "ANN index reasoning"]),
            ("Rejected fine-tuning for entity extraction",
             "Few-shot prompting matched fine-tuned F1 within 2 points at 10% the cost",
             ["why we did not fine-tune the NER model",
              "prompt versus fine-tune decision"]),
            ("bge-small embeddings chosen over MiniLM",
             "+6 MTEB points at same 384 dim and 10 percent runtime overhead",
             ["text embedding model choice",
              "why this sentence encoder"]),
            ("Cross-encoder reranker added after dense retrieval",
             "Lifts top-3 recall from 0.71 to 0.89 on internal eval set",
             ["why a reranker",
              "two-stage retrieval reasoning"]),
        ],
    },
]

ADVERSARIAL = [
    # (query, expected_decision_what_substring, category)
    ("not the Mongo decision, the relational one",       "PostgreSQL",      "negation"),
    ("opposite of stateless auth — server sessions",     "Redis for session", "negation"),
    ("not the streaming platform, the lightweight RPC",  "NATS",            "vocab_gap"),
    ("hashing scheme resistant to GPU attacks",          "Argon2id",        "paraphrase"),
    ("key-derivation function reasoning",                "Argon2id",        "vocab_gap"),
    ("why we avoid in-browser local storage for tokens", "HttpOnly cookies", "paraphrase"),
    ("authentication flow for mobile apps",              "PKCE",            "paraphrase"),
    ("approximate nearest neighbor structure",           "HNSW",            "vocab_gap"),
    ("two-stage retrieval second pass",                  "Cross-encoder reranker", "paraphrase"),
    ("system that records exceptions from prod",         "Sentry",          "vocab_gap"),
]


def build():
    random.seed(SEED)
    decisions = []
    queries = []
    next_id = 1

    for block in DOMAINS:
        for what, why, qs in block["decisions"]:
            did = f"d{next_id:03d}"
            next_id += 1
            decisions.append({"id": did, "what": what, "why": why, "domain": block["domain"]})
            for q in qs[:1]:
                queries.append({"query": q, "expected_id": did, "category": "paraphrase"})
            if len(qs) > 1:
                queries.append({"query": qs[1], "expected_id": did, "category": "vocab_gap"})

    # exact-match query (uses words from decision)
    sample = random.sample(decisions, 10)
    for d in sample:
        first_word = d["what"].split()[0].lower()
        queries.append({
            "query": f"{first_word} {d['what'].split()[1].lower()} reasoning",
            "expected_id": d["id"],
            "category": "exact",
        })

    # adversarial — find decision by substring
    for q, sub, cat in ADVERSARIAL:
        match = next((d for d in decisions if sub.lower() in d["what"].lower()), None)
        if match:
            queries.append({"query": q, "expected_id": match["id"], "category": cat})

    random.shuffle(queries)

    out = {
        "seed": SEED,
        "decisions": decisions,
        "queries": queries,
        "stats": {
            "n_decisions": len(decisions),
            "n_queries":   len(queries),
            "by_category": {
                cat: sum(1 for q in queries if q["category"] == cat)
                for cat in {q["category"] for q in queries}
            },
        },
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    return out


if __name__ == "__main__":
    o = build()
    print(f"wrote {OUT_PATH}")
    print(f"  decisions: {o['stats']['n_decisions']}")
    print(f"  queries:   {o['stats']['n_queries']}")
    print(f"  by category: {o['stats']['by_category']}")
