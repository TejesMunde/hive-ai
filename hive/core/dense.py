"""
Dense retrieval + RRF fusion for hive.

SQLite-backed embedding cache: lazily computes bge-small embeddings for
new/unseen decisions, stores float32 bytes in `decision_embeddings`.

Hybrid retrieval (`hybrid_rerank`) returns a list of decision_ids formed by
Reciprocal Rank Fusion of TF-IDF and dense cosine ranks. The TF-IDF #1 hit is
pinned at rank 0 only when it is a confident keyword winner (see `PIN_MARGIN`);
dense re-orders the rest of the top-K head.

Reader calls `hybrid_rerank` when ENABLE_DENSE is True (default) and falls
back silently if fastembed is unavailable or embedding fails.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections import defaultdict
from typing import Sequence

try:
    import numpy as np
except ImportError:
    np = None  # dense path degrades gracefully; caller catches in hybrid_rerank

from hive.core.normalize import normalize_tokens

ENABLE_DENSE_ENV = "HIVE_DENSE"
RRF_K = 60
# Dense only re-orders within the TF-IDF top-K head; the TF-IDF tail is kept
# verbatim. Full-corpus RRF let noisy dense ranks pull unrelated docs above
# exact keyword hits (exact Recall@1 100% → 70%). Head-only fusion bounds that.
FUSE_TOP_K = 10
# Pin the TF-IDF #1 hit only when it is a confident winner: its normalized
# IDF-overlap score must beat the runner-up by at least this margin. A dominant
# keyword hit (exact match) is trustworthy; a near-tie (thin paraphrase overlap)
# is not — there, let dense reorder the whole head instead of locking a maybe-
# wrong doc at rank 0. Tuned on eval_corpus: +3.2 R@1 over always-pin, exact 10/10.
PIN_MARGIN = 0.15
MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIM = 384


def _dense_enabled() -> bool:
    if os.environ.get(ENABLE_DENSE_ENV, "1") == "0":
        return False
    try:
        import fastembed  # noqa: F401
        return True
    except ImportError:
        return False


def _embed(texts: Sequence[str]) -> np.ndarray:
    from hive.core.embedder import embed_batch
    return embed_batch(list(texts), model=MODEL_NAME)


def _vector_to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def _blob_to_vector(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


def _ensure_embeddings(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, np.ndarray]:
    """
    Return {decision_id: vector} for every row, computing+caching any misses.
    """
    if not rows:
        return {}

    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    cached_rows = conn.execute(
        f"SELECT decision_id, vector FROM decision_embeddings "
        f"WHERE model=? AND decision_id IN ({placeholders})",
        (MODEL_NAME, *ids),
    ).fetchall()
    cached: dict[str, np.ndarray] = {
        r["decision_id"]: _blob_to_vector(r["vector"]) for r in cached_rows
    }

    missing = [r for r in rows if r["id"] not in cached]
    if missing:
        texts = [f'{r["what"]}. {r["why"] or ""}' for r in missing]
        vecs = _embed(texts)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.executemany(
            "INSERT OR REPLACE INTO decision_embeddings "
            "(decision_id, model, dim, vector, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                (r["id"], MODEL_NAME, DIM, _vector_to_blob(vecs[i]), now)
                for i, r in enumerate(missing)
            ],
        )
        conn.commit()
        for i, r in enumerate(missing):
            cached[r["id"]] = vecs[i]

    return cached


def dense_rank(query: str, embeddings: dict[str, np.ndarray]) -> list[tuple[str, float]]:
    """Return (decision_id, cosine) sorted by cosine desc."""
    if not embeddings or not query.strip():
        return []
    ids = list(embeddings.keys())
    M = np.stack([embeddings[i] for i in ids])
    q = _embed([query])[0]
    scores = M @ q
    order = np.argsort(-scores)
    return [(ids[i], float(scores[i])) for i in order]


def rrf_fuse(ranked_lists: Sequence[list[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """RRF over multiple ranked id lists. Returns (id, fused_score) desc."""
    fused: dict[str, float] = defaultdict(float)
    for lst in ranked_lists:
        for rank, did in enumerate(lst):
            fused[did] += 1.0 / (k + rank + 1)
    return sorted(fused.items(), key=lambda x: -x[1])


def fuse_and_guard(
    tfidf_ranked: list,
    dense_ranked: list,
    tfidf_has_signal: bool,
    tfidf_scores: list | None = None,
    top_k: int = FUSE_TOP_K,
) -> list:
    """
    Shared hybrid core — RRF-fuse TF-IDF + dense into one ranking.

    Works on opaque hashable items — decision ids in production, corpus indices
    in the benchmarks — so `reader`, `bench_recall`, and `bench_rerank` all rank
    through the SAME code path. Do not reimplement this logic anywhere else.

    Fusion strategy:
      - No TF-IDF signal (zero corpus overlap): TF-IDF order is noise, so dense
        ranks alone.
      - Otherwise RRF-fuse dense with the TF-IDF top-K head and keep the TF-IDF
        tail verbatim. The TF-IDF #1 is pinned at rank 0 ONLY when it is a
        confident winner (`tfidf_scores[0] - tfidf_scores[1] >= PIN_MARGIN`);
        otherwise dense reorders the whole head. `tfidf_scores` must be aligned
        to `tfidf_ranked` (descending). When omitted, the pin is unconditional
        (back-compat). `top_k=None` restores full-corpus fusion.
    """
    if not tfidf_has_signal:
        return [item for item, _ in rrf_fuse([dense_ranked])]
    if top_k is None:
        return [item for item, _ in rrf_fuse([tfidf_ranked, dense_ranked])]

    pin = True
    if tfidf_scores is not None and len(tfidf_scores) > 1:
        pin = (tfidf_scores[0] - tfidf_scores[1]) >= PIN_MARGIN

    head = tfidf_ranked[:top_k]
    if pin:
        rest = head[1:]
        rest_set = set(rest)
        dense_rest = [d for d in dense_ranked if d in rest_set]
        fused_head = [head[0]] + [item for item, _ in rrf_fuse([rest, dense_rest])]
    else:
        head_set = set(head)
        dense_head = [d for d in dense_ranked if d in head_set]
        fused_head = [item for item, _ in rrf_fuse([head, dense_head])]
    return fused_head + tfidf_ranked[top_k:]


def _tfidf_overlap_drop(
    rows: list[sqlite3.Row],
    query: str,
    ranked_ids: list[str],
) -> list[str] | None:
    """
    If query has ZERO token overlap with the corpus, the TF-IDF ranking is
    garbage (effectively row order). Signal RRF to skip it.
    Returns None when TF-IDF has no signal; otherwise the input list.
    """
    q = normalize_tokens(query)
    if not q:
        return None
    for r in rows:
        if normalize_tokens(f'{r["what"]}. {r["why"] or ""}') & q:
            return ranked_ids
    return None


def hybrid_rerank(
    conn: sqlite3.Connection,
    query: str,
    rows: list[sqlite3.Row],
    tfidf_ranked_ids: list[str],
    tfidf_scores: list | None = None,
) -> list[str] | None:
    """
    Dense + RRF hybrid rerank.

    `tfidf_scores` (optional) are the TF-IDF relevance scores aligned to
    `tfidf_ranked_ids` (descending); they gate the rank-0 pin (see
    `fuse_and_guard`). Returns ordered list of decision_ids, or None if the
    dense path is unavailable. Caller falls back to tfidf_ranked_ids on None.
    """
    if not _dense_enabled() or not rows or not query.strip():
        return None
    try:
        emb = _ensure_embeddings(conn, rows)
        dense_ranked = [did for did, _ in dense_rank(query, emb)]
        tfidf_has_signal = _tfidf_overlap_drop(rows, query, tfidf_ranked_ids) is not None
        return fuse_and_guard(tfidf_ranked_ids, dense_ranked, tfidf_has_signal, tfidf_scores)
    except Exception:
        return None
