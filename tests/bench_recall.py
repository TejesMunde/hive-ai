"""
Recall benchmark — TF-IDF (current reader) vs bge-small dense vs RRF hybrid.

Eval corpus: tests/eval_corpus.json (from gen_eval_corpus.py).
Metrics:
    Recall@1, Recall@3, MRR, median per-query latency (ms).

Methods:
    tfidf  : IDF-overlap scoring matching hive.core.reader._idf_score
             (set-overlap, not full TF-IDF — mirrors production).
    dense  : bge-small-en-v1.5, cosine via dot product (vectors normalized).
    hybrid : Reciprocal Rank Fusion of tfidf + dense at k_rrf = 60.

Reports per-category breakdown so we see WHERE each method fails.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import defaultdict

import numpy as np

from hive.core.normalize import normalize_tokens
from hive.core.embedder import embed_batch
from hive.core.dense import fuse_and_guard

CORPUS_PATH = os.path.join(os.path.dirname(__file__), "eval_corpus.json")
RRF_K = 60


def load_corpus():
    with open(CORPUS_PATH, encoding="utf-8") as f:
        return json.load(f)


def build_idf(corpus_token_sets):
    n = len(corpus_token_sets)
    df = defaultdict(int)
    for s in corpus_token_sets:
        for t in s:
            df[t] += 1
    return {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}


def tfidf_rank(query_tokens, doc_token_sets, idf):
    """Mirror hive.core.reader._idf_score for each doc. Return ranked indices."""
    if not query_tokens:
        return list(range(len(doc_token_sets)))
    q_weight = sum(idf.get(t, 1.0) for t in query_tokens)
    if q_weight == 0:
        return list(range(len(doc_token_sets)))
    scores = []
    for i, dts in enumerate(doc_token_sets):
        hit = query_tokens & dts
        s = sum(idf.get(t, 1.0) for t in hit) / q_weight
        scores.append((s, i))
    scores.sort(key=lambda x: -x[0])
    return [i for _, i in scores]


def dense_rank(query_vec, doc_matrix):
    scores = doc_matrix @ query_vec
    return list(np.argsort(-scores))


def metrics(ranked_indices, expected_idx):
    """Returns (hit@1, hit@3, reciprocal_rank)."""
    try:
        pos = ranked_indices.index(expected_idx)
    except ValueError:
        return 0, 0, 0.0
    return int(pos == 0), int(pos < 3), 1.0 / (pos + 1)


def run():
    data = load_corpus()
    decisions = data["decisions"]
    queries = data["queries"]
    id_to_idx = {d["id"]: i for i, d in enumerate(decisions)}

    # ── Prep: tokens for TF-IDF ────────────────────────────────────────────
    doc_texts = [f'{d["what"]}. {d["why"]}' for d in decisions]
    doc_tokens = [normalize_tokens(t) for t in doc_texts]
    idf = build_idf(doc_tokens)

    # ── Prep: dense embeddings ─────────────────────────────────────────────
    print("embedding corpus...")
    t0 = time.perf_counter()
    doc_vecs = embed_batch(doc_texts)
    print(f"  corpus embed: {(time.perf_counter()-t0)*1000:.0f}ms ({len(doc_texts)} docs)")
    t0 = time.perf_counter()
    q_vecs = embed_batch([q["query"] for q in queries])
    print(f"  query embed:  {(time.perf_counter()-t0)*1000:.0f}ms ({len(queries)} queries)")

    methods = ["tfidf", "dense", "hybrid"]
    overall = {m: {"h1": 0, "h3": 0, "mrr": 0.0, "lat_ms": []} for m in methods}
    by_cat = {m: defaultdict(lambda: {"h1": 0, "h3": 0, "mrr": 0.0, "n": 0}) for m in methods}

    for qi, q in enumerate(queries):
        expected_idx = id_to_idx[q["expected_id"]]
        cat = q["category"]
        q_tokens = normalize_tokens(q["query"])

        # tfidf
        t0 = time.perf_counter()
        r_tf = tfidf_rank(q_tokens, doc_tokens, idf)
        overall["tfidf"]["lat_ms"].append((time.perf_counter() - t0) * 1000)

        # dense
        t0 = time.perf_counter()
        r_de = dense_rank(q_vecs[qi], doc_vecs)
        overall["dense"]["lat_ms"].append((time.perf_counter() - t0) * 1000)

        # hybrid — production code path (hive.core.dense.fuse_and_guard).
        t0 = time.perf_counter()
        tfidf_has_signal = any((q_tokens & dts) for dts in doc_tokens)
        qw = sum(idf.get(t, 1.0) for t in q_tokens) or 1.0
        tfidf_scores = [sum(idf.get(t, 1.0) for t in (q_tokens & doc_tokens[i])) / qw for i in r_tf]
        r_hy = fuse_and_guard(r_tf, r_de, tfidf_has_signal, tfidf_scores)
        overall["hybrid"]["lat_ms"].append((time.perf_counter() - t0) * 1000)

        for m, ranked in (("tfidf", r_tf), ("dense", r_de), ("hybrid", r_hy)):
            h1, h3, mrr = metrics(ranked, expected_idx)
            overall[m]["h1"] += h1
            overall[m]["h3"] += h3
            overall[m]["mrr"] += mrr
            by_cat[m][cat]["h1"] += h1
            by_cat[m][cat]["h3"] += h3
            by_cat[m][cat]["mrr"] += mrr
            by_cat[m][cat]["n"] += 1

    n = len(queries)

    def pct(x):
        return f"{x / n * 100:5.1f}%"

    print("\n=== OVERALL ===")
    print(f"{'method':<10}{'Recall@1':>10}{'Recall@3':>10}{'MRR':>8}{'p50_ms':>10}{'p95_ms':>10}")
    for m in methods:
        lat = sorted(overall[m]["lat_ms"])
        p50 = lat[len(lat) // 2]
        p95 = lat[int(len(lat) * 0.95)]
        print(f"{m:<10}{pct(overall[m]['h1']):>10}{pct(overall[m]['h3']):>10}"
              f"{overall[m]['mrr']/n:>8.3f}{p50:>10.3f}{p95:>10.3f}")

    print("\n=== BY CATEGORY (Recall@3) ===")
    cats = sorted({q["category"] for q in queries})
    header = f"{'method':<10}" + "".join(f"{c:>14}" for c in cats)
    print(header)
    for m in methods:
        row = f"{m:<10}"
        for c in cats:
            d = by_cat[m][c]
            if d["n"] == 0:
                row += f"{'-':>14}"
            else:
                row += f"{d['h3']/d['n']*100:>10.1f}% ({d['n']:>2})"
        print(row)

    print("\n=== BY CATEGORY (Recall@1) ===")
    print(header)
    for m in methods:
        row = f"{m:<10}"
        for c in cats:
            d = by_cat[m][c]
            if d["n"] == 0:
                row += f"{'-':>14}"
            else:
                row += f"{d['h1']/d['n']*100:>10.1f}% ({d['n']:>2})"
        print(row)


if __name__ == "__main__":
    run()
