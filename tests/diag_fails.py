"""
Print every failing query (hybrid Recall@1 miss) with category, expected
decision, and actual top-3. Used to identify missing synonyms.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict

import numpy as np

from hive.core.normalize import normalize_tokens
from hive.core.embedder import embed_batch

CORPUS_PATH = os.path.join(os.path.dirname(__file__), "eval_corpus.json")
RRF_K = 60


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
    scores = [(sum(idf.get(t, 1.0) for t in (query_tokens & dts)) / q_weight, i)
              for i, dts in enumerate(doc_token_sets)]
    scores.sort(key=lambda x: -x[0])
    return [i for _, i in scores]


def rrf(ranked_lists, k=RRF_K):
    fused = defaultdict(float)
    for lst in ranked_lists:
        for r, idx in enumerate(lst):
            fused[idx] += 1.0 / (k + r + 1)
    return [i for i, _ in sorted(fused.items(), key=lambda x: -x[1])]


def run():
    data = json.load(open(CORPUS_PATH, encoding="utf-8"))
    decisions = data["decisions"]
    queries = data["queries"]
    id_to_idx = {d["id"]: i for i, d in enumerate(decisions)}
    doc_texts = [f'{d["what"]}. {d["why"]}' for d in decisions]
    doc_tokens = [normalize_tokens(t) for t in doc_texts]
    idf = build_idf(doc_tokens)
    doc_vecs = embed_batch(doc_texts)
    q_vecs = embed_batch([q["query"] for q in queries])

    fails_by_cat = defaultdict(list)
    for qi, q in enumerate(queries):
        exp = id_to_idx[q["expected_id"]]
        r_tf = tfidf_rank(normalize_tokens(q["query"]), doc_tokens, idf)
        r_de = list(np.argsort(-(doc_vecs @ q_vecs[qi])))
        r_hy = rrf([r_tf, r_de])
        if r_hy[0] != exp:
            fails_by_cat[q["category"]].append({
                "query": q["query"],
                "expected": decisions[exp]["what"],
                "top1": decisions[r_hy[0]]["what"],
                "top2": decisions[r_hy[1]]["what"] if len(r_hy) > 1 else "",
                "expected_rank": r_hy.index(exp) if exp in r_hy else -1,
            })

    for cat, items in fails_by_cat.items():
        print(f"\n=== {cat} ({len(items)} fails) ===")
        for i, f in enumerate(items[:15]):
            print(f"[{i}] Q: {f['query']}")
            print(f"    expected (rank {f['expected_rank']}): {f['expected'][:70]}")
            print(f"    actual top1: {f['top1'][:70]}")


if __name__ == "__main__":
    run()
