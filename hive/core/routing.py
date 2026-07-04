"""
Phase 5: expertise routing.

Given a task description, rank the project's agents by how relevant their prior
decisions are — so work can be advised to whoever has the most applicable, still-
fresh history. Advisory only: route_task NEVER mutates any row (no auto-assign).

Signal (decay-aware retrieval relevance):

    for each LIVE decision d authored by agent a:
        rel    = IDF-overlap(task, d)            # same scorer as the reader
                 (+ dense cosine blended in when HIVE_DENSE is on)
        weight = effective_confidence(d)         # Phase 4 decay
        agent_score(a) += rel * weight

Stale expertise contributes less than fresh/reinforced expertise — which is why
Phase 4 had to land first.
"""

from __future__ import annotations

from hive.db.setup import get_connection
from hive.core.normalize import normalize_tokens
from hive.core.reader import build_idf, idf_score
from hive.core.decay import effective_confidence

EVIDENCE_K = 3   # top matching decisions surfaced per agent


def route_task(project: str, task: str, top_n: int = 3) -> list[dict]:
    """
    Rank agents for `task`. Returns, best first:

        [ { "agent": str,
            "score": float,
            "evidence": [ {decision_id, what, relevance}, ... ] }, ... ]

    Agents with no relevant decisions are omitted. Returns [] when nothing
    matches. Pure read — no writes.
    """
    q_tokens = normalize_tokens(task)
    if not q_tokens:
        return []

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, what, why, agent, created_at, confidence "
            "FROM decisions WHERE project=? AND archived_at IS NULL",
            (project,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return []

    # IDF over the live project corpus — mirrors the reader's keyword relevance.
    corpus = [normalize_tokens(f'{r["what"]} {r["why"] or ""}') for r in rows]
    idf = build_idf(corpus)

    # Optional dense relevance, blended like the reader's hybrid path.
    dense_rel: dict[str, float] = {}
    try:
        from hive.core.dense import _dense_enabled
        if _dense_enabled():
            from hive.core.embedder import embed_batch
            import numpy as np
            doc_vecs = embed_batch([f'{r["what"]}. {r["why"] or ""}' for r in rows])
            q_vec = embed_batch([task])[0]
            sims = doc_vecs @ q_vec
            dense_rel = {rows[i]["id"]: max(0.0, float(sims[i])) for i in range(len(rows))}
    except Exception:
        dense_rel = {}

    per_agent: dict[str, dict] = {}
    for i, r in enumerate(rows):
        kw = idf_score(q_tokens, corpus[i], idf)
        rel = kw + dense_rel.get(r["id"], 0.0) if dense_rel else kw
        if rel <= 0.0:
            continue
        weight = effective_confidence(r["confidence"], r["created_at"])
        contribution = rel * weight
        agent = r["agent"] or "unknown"
        bucket = per_agent.setdefault(agent, {"agent": agent, "score": 0.0, "_matches": []})
        bucket["score"] += contribution
        bucket["_matches"].append({
            "decision_id": r["id"],
            "what":        r["what"],
            "relevance":   round(contribution, 4),
        })

    ranked = sorted(per_agent.values(), key=lambda b: -b["score"])
    out = []
    for b in ranked[:top_n]:
        evidence = sorted(b["_matches"], key=lambda m: -m["relevance"])[:EVIDENCE_K]
        out.append({
            "agent":    b["agent"],
            "score":    round(b["score"], 4),
            "evidence": evidence,
        })
    return out
