"""
read_memory: hot + warm tier retrieval with TF-IDF + recency ranking.

Hot tier  : open tasks + latest snapshot (always returned, ~500 tok budget).
Warm tier : decisions ranked by query relevance (~2500 tok budget).
Cold tier : reserved for explicit retrieval — not surfaced here.

Ranking (warm decisions):
    score = idf_overlap   +   what_boost   +   recency_boost   +   confidence_boost

    idf_overlap     : sum of IDF(t) for t in query_tokens ∩ doc_tokens,
                      normalised by sum of IDF over all query tokens
    what_boost      : +25% of idf_overlap if the term hit the `what` field
                      (the headline) rather than only the `why`
    recency_boost   : up to +0.05 for newer decisions (linear over corpus)
    confidence_boost: + (confidence - 1.0) * 0.05  (small nudge, decay-ready)

Tiebreak: newer first.
"""

import math

from hive.db.setup import get_connection
from hive.core.normalize import normalize_tokens
from hive.core.audit import log as audit_log
from hive.core.dense import hybrid_rerank

HOT_TOKEN_BUDGET  = 500
WARM_TOKEN_BUDGET = 2500
CHARS_PER_TOKEN   = 4

WHAT_BOOST       = 0.25
RECENCY_WEIGHT   = 0.05
CONFIDENCE_WEIGHT = 0.05

def _tokens(text: str) -> set[str]:
    """Stop-filtered, stemmed token bag. Single source of truth lives in normalize."""
    return normalize_tokens(text)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _build_idf(corpus: list[set[str]]) -> dict[str, float]:
    """Smoothed IDF over the project corpus."""
    n = len(corpus)
    if n == 0:
        return {}
    df: dict[str, int] = {}
    for doc in corpus:
        for tok in doc:
            df[tok] = df.get(tok, 0) + 1
    return {tok: math.log((n + 1) / (count + 1)) + 1 for tok, count in df.items()}


def _idf_score(q_tokens: set[str], doc_tokens: set[str], idf: dict[str, float]) -> float:
    if not q_tokens:
        return 0.0
    q_weight = sum(idf.get(t, 1.0) for t in q_tokens)
    if q_weight == 0:
        return 0.0
    hit = q_tokens & doc_tokens
    return sum(idf.get(t, 1.0) for t in hit) / q_weight


def read_memory(project: str, query: str = "") -> dict:
    """
    Return a token-budgeted context slice for `project`, ranked against `query`.

    Shape:
      {
        "hot":  {"open_tasks": [...], "latest_snapshot": {...} | None},
        "warm": {"decisions": [...]},
        "token_estimate": int
      }
    """
    q_tokens = _tokens(query)
    conn = get_connection()

    try:
        # ── Hot: open tasks ────────────────────────────────────────────────
        task_rows = conn.execute(
            "SELECT id, description, assigned_agent, created_at "
            "FROM open_tasks WHERE project=? AND status='open' "
            "ORDER BY created_at DESC",
            (project,),
        ).fetchall()

        open_tasks = []
        hot_tokens = 0
        for row in task_rows:
            cost = _estimate_tokens(row["description"])
            if hot_tokens + cost > HOT_TOKEN_BUDGET:
                break
            open_tasks.append({
                "id":             row["id"],
                "description":    row["description"],
                "assigned_agent": row["assigned_agent"],
                "created_at":     row["created_at"],
            })
            hot_tokens += cost

        # ── Hot: latest snapshot ───────────────────────────────────────────
        snap_row = conn.execute(
            "SELECT id, file_structure, active_stack, current_module, created_at "
            "FROM snapshots WHERE project=? "
            "ORDER BY created_at DESC LIMIT 1",
            (project,),
        ).fetchone()

        latest_snapshot = None
        if snap_row:
            latest_snapshot = {
                "id":             snap_row["id"],
                "file_structure": snap_row["file_structure"],
                "active_stack":   snap_row["active_stack"],
                "current_module": snap_row["current_module"],
                "created_at":     snap_row["created_at"],
            }
            hot_tokens += _estimate_tokens(snap_row["file_structure"] or "")

        # ── Warm: decisions ranked by TF-IDF + recency + confidence ────────
        dec_rows = conn.execute(
            "SELECT id, what, why, agent, created_at, confidence "
            "FROM decisions WHERE project=? "
            "ORDER BY created_at ASC",
            (project,),
        ).fetchall()

        # Pre-tokenise every decision once.
        prepped = []
        corpus_tokens = []
        for row in dec_rows:
            what_toks     = _tokens(row["what"])
            why_toks      = _tokens(row["why"])
            combined_toks = what_toks | why_toks
            prepped.append((row, what_toks, combined_toks))
            corpus_tokens.append(combined_toks)

        idf = _build_idf(corpus_tokens)
        n_docs = max(len(prepped), 1)

        scored = []
        base_by_id: dict[str, float] = {}
        for idx, (row, what_toks, combined_toks) in enumerate(prepped):
            base = _idf_score(q_tokens, combined_toks, idf)
            base_by_id[row["id"]] = base
            what_hit = _idf_score(q_tokens, what_toks, idf)
            boost = base + (what_hit * WHAT_BOOST)

            # Linear recency boost — newest doc gets +RECENCY_WEIGHT, oldest +0.
            # Only applied when there IS a real overlap; otherwise recency
            # silently picks the newest unrelated doc as "top result".
            if q_tokens and base == 0.0:
                recency = 0.0
            else:
                recency = (idx / max(n_docs - 1, 1)) * RECENCY_WEIGHT if n_docs > 1 else 0.0

            conf = row["confidence"] if row["confidence"] is not None else 1.0
            conf_adj = (conf - 1.0) * CONFIDENCE_WEIGHT

            score = boost + recency + conf_adj
            scored.append((score, row["created_at"], row, what_toks, combined_toks))

        # Sort: score desc, then created_at desc (newest first within tie).
        scored.sort(key=lambda x: (-x[0], x[1]), reverse=False)
        # Trick above doesn't reverse created_at; do it explicitly:
        scored.sort(key=lambda x: (-x[0], -_iso_rank(x[1])))

        # ── Hybrid rerank: RRF(tfidf, dense bge-small) ────────────────────
        # Dense path is optional. Falls back silently to TF-IDF ordering if
        # fastembed missing, env disabled, or any embed/cache error.
        # Only re-rank when there's a real query — empty query keeps recency.
        if q_tokens:
            tfidf_ids = [row["id"] for _s, _ts, row, _wt, _ct in scored]
            score_lookup = {row["id"]: (s, ts, row, wt, ct)
                            for s, ts, row, wt, ct in scored}
            row_objects = [score_lookup[i][2] for i in tfidf_ids]
            # Pure IDF-overlap score per id, aligned to tfidf_ids (descending) —
            # gates the rank-0 pin in fuse_and_guard.
            tfidf_scores = [base_by_id[i] for i in tfidf_ids]
            reranked_ids = hybrid_rerank(conn, query, row_objects, tfidf_ids, tfidf_scores)
            if reranked_ids:
                scored = [score_lookup[i] for i in reranked_ids if i in score_lookup]

        decisions = []
        warm_tokens = 0
        for score, _ts, row, _wt, _ct in scored:
            entry = {
                "id":         row["id"],
                "what":       row["what"],
                "why":        row["why"],
                "agent":      row["agent"],
                "created_at": row["created_at"],
                "confidence": row["confidence"] if row["confidence"] is not None else 1.0,
                "score":      round(score, 3),
            }
            cost = _estimate_tokens(entry["what"] + " " + (entry["why"] or ""))
            if warm_tokens + cost > WARM_TOKEN_BUDGET:
                break
            decisions.append(entry)
            warm_tokens += cost

        result = {
            "hot": {
                "open_tasks":      open_tasks,
                "latest_snapshot": latest_snapshot,
            },
            "warm": {
                "decisions": decisions,
            },
            "token_estimate": hot_tokens + warm_tokens,
        }

        # Day 6: every query is auditable. Captures whether the warm tier
        # produced anything and the top score — enough to spot retrieval rot.
        audit_log(project, "query", {
            "query":          (query or "")[:120],
            "decisions_returned": len(decisions),
            "open_tasks_returned": len(open_tasks),
            "top_score":      (decisions[0]["score"] if decisions else 0.0),
            "token_estimate": result["token_estimate"],
        })
        return result

    finally:
        conn.close()


def _iso_rank(ts: str) -> float:
    """
    Convert ISO timestamp to a sortable float (epoch seconds).
    Cheap parse — avoids datetime import overhead in hot loop after first call.
    """
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0
