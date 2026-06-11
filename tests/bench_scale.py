"""
Scale recall benchmark — does hybrid hold its lead as the corpus grows?

Keeps the labeled eval set (tests/eval_corpus.json: real decisions + queries)
and floods the corpus with K synthetic DISTRACTOR decisions drawn from a
vocabulary disjoint from the labeled answers. Re-runs every labeled query at
K = 0, 100, 500, 1000, 2000 and reports Recall@1/@3, MRR, and per-query p50/p95
latency for tfidf / dense / hybrid.

All three methods rank through the production core
(hive.core.dense.fuse_and_guard) — same code path as the reader.

Run:
    PYTHONIOENCODING=utf-8 python tests/bench_scale.py
"""

from __future__ import annotations

import json
import math
import os
import random
import time
from collections import defaultdict

import numpy as np

from hive.core.normalize import normalize_tokens
from hive.core.embedder import embed_batch
from hive.core.dense import fuse_and_guard

CORPUS_PATH = os.path.join(os.path.dirname(__file__), "eval_corpus.json")
SEED = 1234
K_SWEEP = [0, 100, 500, 1000, 2000]

# Distractor vocabulary — deliberately disjoint from the labeled answers
# (postgres/redis/kafka/jwt/argon2/k8s/...) so each labeled query keeps a
# unique best answer and recall drops reflect ranking, not real ambiguity.
_TOOLS = [
    "Consul", "Vault", "Terraform", "Packer", "Nomad", "Envoy", "Linkerd",
    "Istio", "Cilium", "Calico", "Flux", "Tekton", "Jenkins", "CircleCI",
    "Bazel", "Pants", "Nx", "Turborepo", "Vite", "esbuild", "swc", "Rollup",
    "Parcel", "Deno", "Pulumi", "CloudFormation", "Grafana", "Datadog",
    "Honeycomb", "Jaeger", "Zipkin", "Fluentd", "Cassandra", "Scylla",
    "CockroachDB", "TiDB", "Vitess", "Citus", "Clickhouse", "Pinot", "Druid",
    "Trino", "Snowflake", "Databricks", "Flink", "Beam", "Dagster", "Prefect",
    "Temporal", "Cadence", "Celery", "Sidekiq", "BullMQ", "Pulsar", "Memcached",
    "Hazelcast", "Aerospike", "FoundationDB", "etcd", "Zookeeper", "Keycloak",
    "Ory", "Cloudflare", "Fastly", "Akamai", "Varnish", "Traefik", "Caddy",
    "Nginx", "HAProxy", "Kong", "Tyk", "Gloo", "Backstage", "Sourcegraph",
]
_PURPOSES = [
    "service discovery", "secret management", "infrastructure provisioning",
    "image baking", "service mesh routing", "the CI pipeline", "monorepo builds",
    "asset bundling", "feature flag delivery", "edge rate limiting",
    "blue-green rollout", "canary analysis", "schema migration tooling",
    "data lineage tracking", "change-data-capture", "blob storage tiering",
    "internal developer portal", "code search indexing", "API gateway routing",
    "background job scheduling", "cross-region replication", "cold archive tiering",
    "request shadowing", "config templating", "policy enforcement",
]
_REASONS = [
    "lower operational overhead than the incumbent at our scale",
    "stronger consistency guarantees under partition",
    "native multi-region support without bolt-on tooling",
    "a smaller blast radius during rollout failures",
    "tighter integration with the existing platform",
    "predictable tail latency under burst load",
    "cheaper storage at our retention window",
    "first-class declarative configuration",
    "better observability hooks out of the box",
    "reduced vendor lock-in on the serving path",
]
_TEMPLATES = [
    "{tool} adopted for {purpose}",
    "Migrated to {tool} for {purpose}",
    "{tool} standardized across teams for {purpose}",
    "Replaced legacy stack with {tool} for {purpose}",
    "{tool} chosen to handle {purpose}",
]


def make_distractors(n: int, start_id: int) -> list[dict]:
    rng = random.Random(SEED)
    out, seen = [], set()
    while len(out) < n:
        tool = rng.choice(_TOOLS)
        purpose = rng.choice(_PURPOSES)
        tmpl = rng.choice(_TEMPLATES)
        what = tmpl.format(tool=tool, purpose=purpose)
        if what in seen:
            continue
        seen.add(what)
        why = f"{tool} gave us {rng.choice(_REASONS)}"
        out.append({"id": f"x{start_id + len(out):05d}", "what": what, "why": why})
    return out


def build_idf(corpus_token_sets):
    n = len(corpus_token_sets)
    df = defaultdict(int)
    for s in corpus_token_sets:
        for t in s:
            df[t] += 1
    return {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}


def tfidf_rank(query_tokens, doc_token_sets, idf):
    if not query_tokens:
        return list(range(len(doc_token_sets)))
    q_weight = sum(idf.get(t, 1.0) for t in query_tokens) or 1.0
    scores = [(sum(idf.get(t, 1.0) for t in (query_tokens & dts)) / q_weight, i)
              for i, dts in enumerate(doc_token_sets)]
    scores.sort(key=lambda x: -x[0])
    return [i for _, i in scores]


def metrics(ranked, expected):
    try:
        pos = ranked.index(expected)
    except ValueError:
        return 0, 0, 0.0
    return int(pos == 0), int(pos < 3), 1.0 / (pos + 1)


def run():
    data = json.load(open(CORPUS_PATH, encoding="utf-8"))
    real = data["decisions"]
    queries = data["queries"]
    pad = make_distractors(max(K_SWEEP), start_id=1)

    print(f"labeled: {len(real)} decisions, {len(queries)} queries")
    print(f"distractor pool: {len(pad)}\n")

    # Embed every text once (real + full distractor pool); slice per K.
    all_docs = real + pad
    all_texts = [f'{d["what"]}. {d["why"]}' for d in all_docs]
    print("embedding corpus (one-time)...")
    t0 = time.perf_counter()
    all_vecs = embed_batch(all_texts)
    print(f"  embedded {len(all_texts)} docs in {(time.perf_counter()-t0):.1f}s")
    q_vecs = embed_batch([q["query"] for q in queries])
    all_tokens = [normalize_tokens(t) for t in all_texts]

    print(f"\n{'K':>6} {'method':<8}{'R@1':>8}{'R@3':>8}{'MRR':>8}{'p50ms':>9}{'p95ms':>9}")
    print("-" * 56)

    for K in K_SWEEP:
        docs = real + pad[:K]
        n = len(docs)
        tok = all_tokens[:len(real)] + all_tokens[len(real):len(real) + K]
        vecs = np.vstack([all_vecs[:len(real)], all_vecs[len(real):len(real) + K]]) if K else all_vecs[:len(real)]
        idf = build_idf(tok)
        id_to_idx = {d["id"]: i for i, d in enumerate(docs)}

        agg = {m: {"h1": 0, "h3": 0, "mrr": 0.0, "lat": []} for m in ("tfidf", "dense", "hybrid")}
        for qi, q in enumerate(queries):
            if q["expected_id"] not in id_to_idx:
                continue
            e = id_to_idx[q["expected_id"]]
            qt = normalize_tokens(q["query"])

            t0 = time.perf_counter()
            r_tf = tfidf_rank(qt, tok, idf)
            agg["tfidf"]["lat"].append((time.perf_counter() - t0) * 1000)

            t0 = time.perf_counter()
            r_de = list(np.argsort(-(vecs @ q_vecs[qi])))
            agg["dense"]["lat"].append((time.perf_counter() - t0) * 1000)

            t0 = time.perf_counter()
            sig = any(qt & dts for dts in tok)
            qw = sum(idf.get(t, 1.0) for t in qt) or 1.0
            tfidf_scores = [sum(idf.get(t, 1.0) for t in (qt & tok[i])) / qw for i in r_tf]
            r_hy = fuse_and_guard(r_tf, r_de, sig, tfidf_scores)
            agg["hybrid"]["lat"].append((time.perf_counter() - t0) * 1000)

            for m, r in (("tfidf", r_tf), ("dense", r_de), ("hybrid", r_hy)):
                h1, h3, mrr = metrics(r, e)
                agg[m]["h1"] += h1
                agg[m]["h3"] += h3
                agg[m]["mrr"] += mrr

        nq = len(queries)
        for m in ("tfidf", "dense", "hybrid"):
            lat = sorted(agg[m]["lat"])
            p50 = lat[len(lat) // 2]
            p95 = lat[int(len(lat) * 0.95)]
            print(f"{n:>6} {m:<8}{agg[m]['h1']/nq*100:>7.1f}%{agg[m]['h3']/nq*100:>7.1f}%"
                  f"{agg[m]['mrr']/nq:>8.3f}{p50:>9.3f}{p95:>9.3f}")
        print()


if __name__ == "__main__":
    run()
