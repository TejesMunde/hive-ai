"""
Reranker bench — RRF top-K then cross-encoder rerank.

Measures Recall@1 + Recall@3 delta from adding a cross-encoder pass to the
hybrid pipeline. Reranker scores each (query, candidate_doc) pair directly
and re-orders top-K from RRF.

Model: Xenova/ms-marco-MiniLM-L-6-v2 (ONNX via fastembed). 22M params.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hive.core.normalize import normalize_tokens
from hive.core.embedder import embed_batch
from hive.core.dense import fuse_and_guard

CORPUS_PATH = os.path.join(os.path.dirname(__file__), "eval_corpus.json")
RRF_K = 60
RERANK_TOP_K = 10
CE_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


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
    return list(np.argsort(-(doc_matrix @ query_vec)))


def metrics(ranked_indices, expected_idx):
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

    doc_texts = [f'{d["what"]}. {d["why"]}' for d in decisions]
    doc_tokens = [normalize_tokens(t) for t in doc_texts]
    idf = build_idf(doc_tokens)

    print("embedding corpus...")
    doc_vecs = embed_batch(doc_texts)
    q_vecs = embed_batch([q["query"] for q in queries])

    print(f"loading cross-encoder {CE_MODEL}...")
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    ce = TextCrossEncoder(model_name=CE_MODEL)

    methods = ["hybrid", "hybrid+ce"]
    overall = {m: {"h1": 0, "h3": 0, "mrr": 0.0, "lat_ms": []} for m in methods}
    by_cat = {m: defaultdict(lambda: {"h1": 0, "h3": 0, "n": 0}) for m in methods}

    for qi, q in enumerate(queries):
        expected_idx = id_to_idx[q["expected_id"]]
        cat = q["category"]
        q_tokens = normalize_tokens(q["query"])

        # hybrid — production code path (hive.core.dense.fuse_and_guard).
        t0 = time.perf_counter()
        r_tf = tfidf_rank(q_tokens, doc_tokens, idf)
        r_de = dense_rank(q_vecs[qi], doc_vecs)
        tfidf_has_signal = any((q_tokens & dts) for dts in doc_tokens)
        qw = sum(idf.get(t, 1.0) for t in q_tokens) or 1.0
        tfidf_scores = [sum(idf.get(t, 1.0) for t in (q_tokens & doc_tokens[i])) / qw for i in r_tf]
        r_hy = fuse_and_guard(r_tf, r_de, tfidf_has_signal, tfidf_scores)
        overall["hybrid"]["lat_ms"].append((time.perf_counter() - t0) * 1000)

        # hybrid + cross-encoder rerank top-K
        t0 = time.perf_counter()
        top_k = r_hy[:RERANK_TOP_K]
        candidate_texts = [doc_texts[i] for i in top_k]
        ce_scores = list(ce.rerank(q["query"], candidate_texts))
        order = sorted(range(len(top_k)), key=lambda i: -ce_scores[i])
        r_ce = [top_k[i] for i in order] + r_hy[RERANK_TOP_K:]
        overall["hybrid+ce"]["lat_ms"].append((time.perf_counter() - t0) * 1000)

        for m, ranked in (("hybrid", r_hy), ("hybrid+ce", r_ce)):
            h1, h3, mrr = metrics(ranked, expected_idx)
            overall[m]["h1"] += h1
            overall[m]["h3"] += h3
            overall[m]["mrr"] += mrr
            by_cat[m][cat]["h1"] += h1
            by_cat[m][cat]["h3"] += h3
            by_cat[m][cat]["n"] += 1

    n = len(queries)
    print("\n=== OVERALL ===")
    print(f"{'method':<12}{'Recall@1':>10}{'Recall@3':>10}{'MRR':>8}{'p50_ms':>10}{'p95_ms':>10}")
    for m in methods:
        lat = sorted(overall[m]["lat_ms"])
        p50 = lat[len(lat) // 2]
        p95 = lat[int(len(lat) * 0.95)]
        print(f"{m:<12}{overall[m]['h1']/n*100:>9.1f}%{overall[m]['h3']/n*100:>9.1f}%"
              f"{overall[m]['mrr']/n:>8.3f}{p50:>10.3f}{p95:>10.3f}")

    print("\n=== Recall@1 BY CATEGORY ===")
    cats = sorted({q["category"] for q in queries})
    print(f"{'method':<12}" + "".join(f"{c:>14}" for c in cats))
    for m in methods:
        row = f"{m:<12}"
        for c in cats:
            d = by_cat[m][c]
            if d["n"]:
                row += f"{d['h1']/d['n']*100:>10.1f}% ({d['n']:>2})"
            else:
                row += f"{'-':>14}"
        print(row)

    print("\n=== Recall@3 BY CATEGORY ===")
    print(f"{'method':<12}" + "".join(f"{c:>14}" for c in cats))
    for m in methods:
        row = f"{m:<12}"
        for c in cats:
            d = by_cat[m][c]
            if d["n"]:
                row += f"{d['h3']/d['n']*100:>10.1f}% ({d['n']:>2})"
            else:
                row += f"{'-':>14}"
        print(row)


if __name__ == "__main__":
    run()
