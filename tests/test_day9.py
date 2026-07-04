"""
Day 9 / Phase 4 — confidence decay, cold archive, contradiction detection v2.

Run: PYTHONIOENCODING=utf-8 python tests/test_day9.py

Self-contained: throwaway DB via HIVE_DB_PATH. Dense OFF for the core flow so it
stays fast/stdlib; the contradiction-v2 dense path is checked separately and
skips cleanly when fastembed is unavailable.
"""

import os
import shutil
import tempfile
from datetime import datetime, timezone, timedelta

_TMP = tempfile.mkdtemp(prefix="hive_day9_")
os.environ["HIVE_DB_PATH"] = os.path.join(_TMP, "day9.db")
os.environ.setdefault("HIVE_DENSE", "0")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hive import (
    init_db, write_memory, read_memory, get_provenance,
    reinforce_decision, archive_decision, unarchive_decision, sweep_archive,
    get_connection,
)
from hive.core.decay import effective_confidence, HALF_LIFE_DAYS, ARCHIVE_FLOOR


def _passed(label):
    print(f"  [OK ] {label}")


def _age_decision(decision_id, days):
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_connection()
    conn.execute("UPDATE decisions SET created_at=? WHERE id=?", (ts, decision_id))
    conn.commit()
    conn.close()


def main():
    init_db()
    project = "phase4"

    print("\n--- Confidence decay (pure read-time function) ---")
    now = datetime.now(timezone.utc)
    assert effective_confidence(1.0, now.isoformat(), now) == 1.0
    half = (now - timedelta(days=HALF_LIFE_DAYS)).isoformat()
    assert abs(effective_confidence(1.0, half) - 0.5) < 1e-6, "half-life math wrong"
    assert abs(effective_confidence(0.8, half) - 0.4) < 1e-6  # decay is linear in stored
    _passed("eff_conf: age0 -> stored, half-life -> half")

    dec = write_memory("decision", project, {
        "what": "Chose PostgreSQL as the primary OLTP store",
        "why":  "ACID guarantees and JSONB fit our workload",
        "agent": "claude-code",
    })
    did = dec["id"]
    conn = get_connection()
    stored = conn.execute("SELECT confidence FROM decisions WHERE id=?", (did,)).fetchone()["confidence"]
    conn.close()
    assert stored == 1.0, "stored confidence should be untouched by reads"
    read_memory(project, query="primary store")  # a read must NOT mutate confidence
    conn = get_connection()
    stored2 = conn.execute("SELECT confidence FROM decisions WHERE id=?", (did,)).fetchone()["confidence"]
    conn.close()
    assert stored2 == 1.0, "read_memory mutated stored confidence"
    _passed("read_memory does not mutate stored confidence")

    print("\n--- Reinforcement caps at 1.0, resets the clock, un-archives ---")
    _age_decision(did, 400)  # far past the floor
    assert sweep_archive(project) == [did], "stale decision not swept"
    assert len(read_memory(project, query="primary store")["warm"]["decisions"]) == 0
    rr = reinforce_decision(did)
    # confidence is a freshness/trust signal in [0,1]; a full decision stays 1.0
    # (no immunity reserve), but its decay clock is reset so it revives.
    assert rr["status"] == "reinforced" and rr["confidence"] == 1.0, rr
    live = read_memory(project, query="primary store")["warm"]["decisions"]
    assert len(live) == 1 and live[0]["id"] == did, "reinforce did not revive decision"
    assert live[0]["effective_confidence"] == 1.0, "clock not reset on reinforce"
    _passed("reinforce a full decision: stays 1.0, clock reset, un-archived")

    # A tentative (sub-1.0) decision: reinforcement bumps it toward the ceiling.
    tent = write_memory("decision", project, {
        "what": "Tentatively use gRPC for the internal RPC layer",
        "why": "needs a load test before we fully commit", "confidence": 0.5})
    tid = tent["id"]
    assert reinforce_decision(tid)["confidence"] == 0.75, "0.5 + 0.25 should be 0.75"
    assert reinforce_decision(tid)["confidence"] == 1.0, "0.75 + 0.25 should clamp to 1.0"
    _passed("reinforce a tentative decision: 0.5 -> 0.75 -> capped 1.0")

    # The [0,1] invariant: out-of-range writes are clamped, never stored as-is.
    write_memory("decision", project, {
        "what": "Decision written with an out-of-range high confidence",
        "why": "guard against bad caller input", "confidence": 5.0})
    write_memory("decision", project, {
        "what": "Decision written with a negative confidence value here",
        "why": "guard against bad caller input", "confidence": -3.0})
    conn = get_connection()
    confs = [r["confidence"] for r in conn.execute("SELECT confidence FROM decisions")]
    conn.close()
    assert all(0.0 <= c <= 1.0 for c in confs), f"confidence escaped [0,1]: {confs}"
    _passed("confidence invariant: writes clamped + reinforcement capped to [0,1]")

    print("\n--- Cold archive triggers ---")
    # explicit. (read_memory returns all live decisions, budget-limited — assert
    # on membership of this id, not the total count.)
    def warm_ids(**kw):
        ctx = read_memory(project, query="session cache redis", **kw)
        return {d["id"] for d in ctx["warm"]["decisions"]}

    a = write_memory("decision", project, {
        "what": "Selected Redis for the session cache layer", "why": "sub-ms reads"})
    aid = a["id"]
    assert aid in warm_ids(), "fresh decision should be live"
    assert archive_decision(aid)["status"] == "archived"
    assert aid not in warm_ids(), "archived decision still in warm tier"
    assert unarchive_decision(aid)["status"] == "unarchived"
    assert aid in warm_ids(), "unarchived decision not back in warm tier"
    _passed("explicit archive / unarchive")

    # superseded -> auto-archive
    sup = write_memory("decision", project, {
        "what": "Migrated the session cache from Redis to Memcached",
        "why":  "simpler ops for our single-region setup",
        "supersedes_id": aid,
    })
    assert sup["status"] == "committed"
    conn = get_connection()
    arch_at = conn.execute("SELECT archived_at FROM decisions WHERE id=?", (aid,)).fetchone()["archived_at"]
    conn.close()
    assert arch_at is not None, "superseded decision not auto-archived"
    _passed("superseding a decision auto-archives the old one")

    # include_archived flag + provenance still resolves
    incl = read_memory(project, query="session cache redis", include_archived=True)["warm"]["decisions"]
    assert any(d["id"] == aid for d in incl), "include_archived did not surface archived row"
    assert get_provenance(aid) is not None, "provenance must resolve archived decisions"
    _passed("include_archived flag + provenance resolves archived")

    print("\n--- Contradiction detection v2 (dense path, optional) ---")
    try:
        import fastembed  # noqa: F401
        os.environ["HIVE_DENSE"] = "1"  # enable the dense path for this block
        from hive.core.guard import _find_contradiction_dense
        import json
        corpus = json.load(open(os.path.join(os.path.dirname(__file__), "eval_corpus.json"), encoding="utf-8"))
        whats = [x["what"] for x in corpus["decisions"]]
        fps = sum(1 for i, w in enumerate(whats) if _find_contradiction_dense(w, whats[:i] + whats[i+1:]))
        assert fps == 0, f"contradiction v2 has {fps} false positives on eval corpus"
        caught = _find_contradiction_dense("Migrated the public API layer to gRPC",
                                           ["Adopted REST for the public API layer"])
        assert caught is not None, "v2 missed a reworded contradiction"
        comp = _find_contradiction_dense("JWT chosen for stateless auth across services",
                                         ["Refresh tokens stored in HttpOnly cookies for the browser"])
        assert comp is None, "v2 flagged complementary decisions"
        _passed("v2: 0 false positives, catches reword, ignores complementary")
    except ImportError:
        _passed("v2 dense path skipped (fastembed not installed) — degrades to v1")

    print("\n------------------------------------------------------------")
    print("  Day 9 / Phase 4 complete — all assertions passed")
    print("------------------------------------------------------------")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
