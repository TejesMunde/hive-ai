# Phase 2 — Semantic Retrieval (Hybrid TF-IDF + Dense)

**Status:** ✅ Shipped
**Constraint lifted:** Phase 1 was pure stdlib. Phase 2 adds `numpy` + `fastembed`
(ONNX runtime). The TF-IDF path must still work when `fastembed` is absent.
**Milestone test:** `tests/bench_recall.py` + `tests/bench_scale.py` — hybrid beats
the TF-IDF baseline on every metric and holds that lead across 54× corpus growth.

---

## 1. Goal of Phase 2

From the roadmap:

> *"Semantic embeddings (bge-small), hybrid retrieval."*

Phase 1 answered queries with keyword retrieval (TF-IDF + a curated stemmer and
synonym map). The synonym map did a lot of the semantic work, so the bar for
embeddings was high: dense retrieval had to *beat* an already-strong keyword
baseline, not just exist.

The acceptance bar: **hybrid ≥ TF-IDF on Recall@1, Recall@3, and MRR**, and the
win must survive a corpus far larger than the 38-doc eval set.

---

## 2. Architecture (final state)

```
hive/core/
├── embedder.py   # fastembed ONNX wrapper, bge-small-en-v1.5 (384-dim,
│                 #   L2-normalized → cosine = dot). Lazy load, per-process cache.
├── dense.py      # dense cosine ranking + RRF hybrid core (fuse_and_guard)
└── reader.py     # calls hybrid_rerank when a real query is present

tests/
├── bench_recall.py  # tfidf vs dense vs hybrid: Recall@1/@3, MRR, latency
├── bench_rerank.py  # cross-encoder rerank delta over hybrid
├── bench_scale.py   # recall + latency vs corpus size (K synthetic distractors)
├── eval_corpus.json # 38 labeled decisions / 96 queries, 5 categories
└── gen_eval_corpus.py
```

### Storage

One new table, `decision_embeddings` (added in the Phase 1 schema for
forward-compat): one row per decision, `float32` vector stored as a little-endian
BLOB with `model` + `dim` so a model swap re-embeds cleanly. Cache is lazy —
`_ensure_embeddings` computes and stores any miss on first query.

### The hybrid core — `fuse_and_guard`

Single source of truth for hybrid ranking. Works on opaque hashable items
(decision ids in production, corpus indices in the benches) so `reader`,
`bench_recall`, `bench_rerank`, and `bench_scale` all rank through the SAME code:

```
no TF-IDF signal (zero corpus overlap)  → dense ranks alone
otherwise → pin the TF-IDF #1 hit at rank 0,
            RRF-fuse dense with TF-IDF across the rest of the top-K head
            (FUSE_TOP_K = 10), keep the TF-IDF tail verbatim below
```

`reader.hybrid_rerank` returns `None` (silent fallback to TF-IDF order) when
`fastembed` is missing, `HIVE_DENSE=0`, or any embed/cache error.

---

## 3. Build Log

### Step 1 — Reconcile the benchmarks (they disagreed)

The two benches reported different "hybrid" numbers: `bench_recall` had it losing
to TF-IDF (R@1 65.6), `bench_rerank` had it winning (R@1 74.0). Cause: each
reimplemented hybrid privately. `bench_rerank` used naive `rrf([tf, de])` with no
guards; `bench_recall` mirrored production.

Fix: extracted the shared `fuse_and_guard` core into `dense.py`; both benches and
the production reader now call it. The inflated number vanished — one honest
result. (Also deleted both private `rrf` copies; removed `_apply_negation_penalty`
in favour of the shared path.)

### Step 2 — First reconciled baseline: hybrid LOST

With benches aligned, hybrid was 65.6 / 78.1 / 0.731 vs TF-IDF 74.0 / 83.3 / 0.803
— worse on every metric. Two distinct causes found:

- **RRF diluted exact hits.** Full-corpus RRF let a noisy dense rank pull a
  distractor above an exact keyword hit: `exact` Recall@1 100% → 70%.
- **The negation guard back-fired.** It demoted any doc sharing a token with the
  4 words after a negation marker. Markers included `rejected` — so the eval
  query *"rejected django"* (asking ABOUT the rejection decision) buried the
  django doc itself (tf rank 0 → rank 21).

### Step 3 — Fix 1: pin TF-IDF #1 + top-K head fusion

`fuse_and_guard` now pins the single best TF-IDF hit at rank 0 and lets dense
reorder only the rest of the TF-IDF top-K (`FUSE_TOP_K=10`) head; the tail is kept
verbatim. Bounds dense's blast radius to the head. Restored `exact` Recall@1 to
100%.

### Step 4 — Fix 2: remove the negation guard (net-harmful)

Probed guard on vs off across all 96 queries:

```
neg ON   R@1 69.8%  R@3 85.4%  MRR 0.775
neg OFF  R@1 76.0%  R@3 90.6%  MRR 0.834
```

The token-overlap demotion buried more correct docs than it helped (the
"why NOT X" case is rare; the collateral damage was broad). Removed the machinery
entirely (`_NEG_MARKERS`, `_NEG_RE`, `_negated_tokens`, `import re`). If a precise
"exclude X" feature is ever wanted, target only the single chosen-X doc — never
token-overlap demotion.

### Step 5 — Cross-encoder evaluated and rejected

`bench_rerank.py` adds a `Xenova/ms-marco-MiniLM-L-6-v2` rerank pass over the
hybrid top-10. Result: worse on every metric (R@1 76.0 → 69.8) and ~250× slower
(0.1 ms → 27 ms p50); it also zeroed `negation` Recall@1. Kept in the bench as
evidence only — **not** wired into the reader.

### Step 6 — Scale validation

`bench_scale.py` floods the corpus with K disjoint-vocabulary distractor
decisions (K = 0…2000) and re-runs the 96 labeled queries:

```
   K   hybrid R@1/R@3/MRR   dense R@1/R@3   hybrid fuse p50   tfidf p50
  38   76.0 / 90.6 / 0.834   60.4 / 80.2     0.02 ms           0.03 ms
2038   78.1 / 89.6 / 0.832   49.0 / 58.3     0.12 ms           1.20 ms
```

- Hybrid recall is **flat across 54× growth** — not overfit to 38 docs.
- **Dense-alone collapses** (R@1 60 → 49) as it drifts to adjacent distractors;
  hybrid is immune because the pinned TF-IDF #1 anchors the top result.
- Scaling bottleneck is the **TF-IDF pure-Python loop** (1.2 ms p50 at ~2k docs),
  not dense (0.2 ms, vectorized). Vectorize TF-IDF before pushing past ~10k.

---

## 4. Final Numbers (`bench_recall.py` / `bench_rerank.py`, 38 docs / 96 queries)

| method                | Recall@1 | Recall@3 |   MRR | p50_ms |
|-----------------------|----------|----------|-------|--------|
| tfidf                 |  74.0%   |  83.3%   | 0.803 |  0.03  |
| dense                 |  60.4%   |  80.2%   | 0.721 |  0.01  |
| **hybrid (default)**  | **79.2%**| **91.7%**| **0.856** | 0.10 |
| hybrid + cross-encoder|  69.8%   |  84.4%   | 0.779 | 27.1   |

Per category, hybrid ≥ TF-IDF everywhere: exact 100/100, negation R@3 100,
paraphrase 85.7/92.9, vocab_gap 69.0/88.1.

### Post-ship refinement — confidence-gated pin (2026-06-11)

A failure analysis showed 100% of R@1 misses were paraphrase/vocab_gap; the
paraphrase cluster sat at rank 2 because the unconditional rank-0 pin locked a
thin-overlap keyword doc. Made the pin confidence-gated (`PIN_MARGIN=0.15`): pin
the TF-IDF #1 only when it beats #2 by ≥0.15 normalized overlap, else dense
reorders the whole head. Lifted hybrid R@1 76.0 → 79.2 (R@3 90.6 → 91.7, MRR
0.834 → 0.856), exact still 100, flat at 2k-doc scale. An embedding-model A/B
(bge-base/large) was run and rejected — bge-base gives the hybrid no gain,
bge-large +2 R@1 at 40× size; bge-small stays default.

---

## 5. Key Design Decisions

| Decision | Reason |
|---|---|
| `BAAI/bge-small-en-v1.5` over `all-mpnet-base-v2` | 384-dim, 33 MB, MIT, MTEB 62.2. ONNX via fastembed — no torch. Supersedes the earlier Phase 1 plan. |
| One shared `fuse_and_guard` core | Benches that reimplement hybrid drift from production and lie. One code path or no trust. |
| Pin TF-IDF #1, fuse dense over top-K head | Full-corpus RRF dropped exact Recall@1 100% → 70%. Pinning + head-only fusion makes hybrid win on every metric — and the pin is what makes it scale. |
| Removed the negation guard | Token-overlap demotion buried correct docs (−6 pts R@1). Net-harmful at this corpus; rare upside. |
| Cross-encoder rejected | Worse on every metric, ~250× slower. The dense+pin hybrid already captures the recall the reranker was meant to add. |
| Dense path stays optional | `HIVE_DENSE=0` / missing fastembed must degrade cleanly to TF-IDF. No hard dependency on ONNX at import time. |

---

## 6. Known Limits (deferred)

| Limit | Resolved in |
|---|---|
| TF-IDF scoring is a pure-Python loop — O(N) per query, the latency bottleneck past ~10k docs | future perf pass (vectorize / candidate-cap) |
| No precise "exclude X" query support (negation guard removed) | revisit only with single-doc targeting |
| `decision_embeddings` re-embeds on model swap but has no background backfill job | Phase 3+ |
| Eval corpus is synthetic (38 real + distractors); no production-traffic eval yet | when real usage data exists |

---

## 7. Phase 2 Acceptance Criteria — all met

- [x] Dense embeddings computed + cached per decision (`decision_embeddings`)
- [x] Hybrid retrieval beats the TF-IDF baseline on Recall@1, Recall@3, and MRR
- [x] The win holds across 54× corpus growth (38 → 2038 docs)
- [x] Dense path is optional — degrades to TF-IDF when fastembed/HIVE_DENSE off
- [x] All benches rank through one production core (no private reimplementations)
- [x] Cross-encoder evaluated with a reproducible bench and explicitly rejected
- [x] `test_day1.py … test_day7.py` still green; milestone unchanged (15/15, MRR 1.000)

**Phase 2 shippable.** Ready to start Phase 3 (dead-ends table, decision
provenance, agent global config) when the user gives the go-ahead.
