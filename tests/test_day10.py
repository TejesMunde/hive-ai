"""
Day 10 / Phase 5 — agent handoff packets + expertise routing.

Run: PYTHONIOENCODING=utf-8 python tests/test_day10.py

Self-contained: throwaway DB via HIVE_DB_PATH. Dense OFF so it stays fast/stdlib;
routing's keyword path is exercised directly (dense blend is additive + optional).
"""

import os
import shutil
import tempfile
import time
from datetime import datetime, timezone, timedelta

_TMP = tempfile.mkdtemp(prefix="hive_day10_")
os.environ["HIVE_DB_PATH"] = os.path.join(_TMP, "day10.db")
os.environ.setdefault("HIVE_DENSE", "0")

from hive import (
    init_db, write_memory, close_task,
    create_handoff, get_handoff, latest_handoff, route_task,
    get_connection,
)


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
    project = "phase5"

    print("\n--- Handoff: first packet reports full state + history ---")
    d1 = write_memory("decision", project, {
        "what": "Chose PostgreSQL for the primary OLTP store",
        "why":  "ACID + JSONB fit the workload", "agent": "alice"})
    t1 = write_memory("open_task", project, {
        "description": "Set up the database migration tooling", "agent": "alice"})
    h1 = create_handoff(project, "alice", "bob")
    assert h1["delta"]["since"] is None, "first handoff should have since=None"
    assert len(h1["delta"]["decisions_added"]) == 1
    assert len(h1["delta"]["tasks_opened"]) == 1
    assert h1["state"]["open_tasks"], "state should include the open task"
    assert any(d["id"] == d1["id"] for d in h1["state"]["top_decisions"])
    _passed("first handoff: since=None, full state + history")

    print("\n--- Handoff: delta is bounded to since-last-handoff ---")
    time.sleep(0.01)
    d2 = write_memory("decision", project, {
        "what": "Added Redis cache for session token lookups",
        "why":  "sub-millisecond reads", "agent": "bob"})
    close_task(t1["id"])
    de = write_memory("dead_end", project, {
        "what_tried": "Evaluated MongoDB for the primary store",
        "why_failed": "no multi-document transactions"})
    h2 = create_handoff(project, "bob", "carol")
    assert h2["delta"]["since"] == h1["created_at"], "delta boundary should be h1"
    ids = {d["id"] for d in h2["delta"]["decisions_added"]}
    assert ids == {d2["id"]}, f"delta should contain ONLY the new decision, got {ids}"
    assert len(h2["delta"]["tasks_closed"]) == 1 and h2["delta"]["tasks_closed"][0]["id"] == t1["id"]
    assert len(h2["delta"]["dead_ends_added"]) == 1
    _passed("second handoff: delta only since h1 (decision, closed task, dead end)")

    print("\n--- Handoff: empty delta when nothing changed ---")
    h3 = create_handoff(project, "carol", "dave")
    assert not h3["delta"]["decisions_added"]
    assert not h3["delta"]["tasks_closed"]
    assert not h3["delta"]["dead_ends_added"]
    _passed("consecutive handoff with no activity -> empty delta")

    print("\n--- Handoff: persistence + round-trip ---")
    assert latest_handoff(project)["id"] == h3["id"]
    assert get_handoff(h1["id"])["id"] == h1["id"]
    assert get_handoff("nope") is None
    _passed("get_handoff / latest_handoff round-trip")

    print("\n--- Routing: ranks the most relevant agent with evidence ---")
    ranked = route_task(project, "how do we handle database storage and migrations")
    assert ranked, "expected at least one ranked agent"
    assert ranked[0]["agent"] == "alice", f"alice (db author) should rank top, got {ranked}"
    assert ranked[0]["evidence"], "top agent should carry evidence"
    assert any("PostgreSQL" in e["what"] for e in ranked[0]["evidence"])
    _passed("route_task ranks the db author top, with evidence")

    print("\n--- Routing: no mutation, empty on no match ---")
    conn = get_connection()
    before = conn.execute("SELECT id, agent FROM decisions").fetchall()
    conn.close()
    route_task(project, "database migrations")  # must not write anything
    conn = get_connection()
    after = conn.execute("SELECT id, agent FROM decisions").fetchall()
    conn.close()
    assert [dict(r) for r in before] == [dict(r) for r in after], "route_task mutated state"
    assert route_task(project, "zzz qqq xyzzy unrelated") == []
    _passed("route_task is read-only; [] on no match")

    print("\n--- Routing: decay-aware (fresh on-topic beats stale on-topic) ---")
    rp = "phase5-decay"
    fresh = write_memory("decision", rp, {
        "what": "Adopted Kafka as the streaming event backbone for billing",
        "why":  "replay semantics and partition ordering", "agent": "fresh_expert"})
    stale = write_memory("decision", rp, {
        "what": "Tuned Kafka consumer group rebalancing for throughput",
        "why":  "reduce duplicate processing under load", "agent": "stale_expert"})
    assert fresh["status"] == "committed" and stale["status"] == "committed", (fresh, stale)
    _age_decision(stale["id"], 540)  # ~3 half-lives -> heavily decayed
    rr = route_task(rp, "event streaming platform with kafka")
    agents = [a["agent"] for a in rr]
    assert agents and agents[0] == "fresh_expert", f"fresh should outrank stale, got {agents}"
    fresh_score = next(a["score"] for a in rr if a["agent"] == "fresh_expert")
    stale_score = next(a["score"] for a in rr if a["agent"] == "stale_expert")
    assert fresh_score > stale_score, (fresh_score, stale_score)
    _passed("decay applied: fresh_expert outranks stale_expert on the same topic")

    print("\n------------------------------------------------------------")
    print("  Day 10 / Phase 5 complete — all assertions passed")
    print("------------------------------------------------------------")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
