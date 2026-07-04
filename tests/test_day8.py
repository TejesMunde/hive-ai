"""
Day 8 / Phase 3 — dead ends, decision provenance, idempotent global config.

Run: PYTHONIOENCODING=utf-8 python tests/test_day8.py

Self-contained: uses a throwaway DB via HIVE_DB_PATH and a temp dir for the
global-config target. Dense path off so the test stays pure-stdlib and fast.
"""

import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="hive_day8_")
os.environ["HIVE_DB_PATH"] = os.path.join(_TMP, "day8.db")
os.environ.setdefault("HIVE_DENSE", "0")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pathlib import Path

from hive import init_db, write_memory, read_memory, get_provenance
from hive.db.setup import get_connection
from hive.cli.init import init_global_config, BEGIN, END


def _passed(label):
    print(f"  [OK ] {label}")


def main():
    init_db()
    project = "phase3"

    print("\n--- Schema ---")
    conn = get_connection()
    dec_cols = {r["name"] for r in conn.execute("PRAGMA table_info(decisions)")}
    de_cols = {r["name"] for r in conn.execute("PRAGMA table_info(dead_ends)")}
    conn.close()
    assert "supersedes_id" in dec_cols, "decisions.supersedes_id missing"
    assert {"what_tried", "why_failed", "chosen_decision_id"} <= de_cols, "dead_ends schema wrong"
    _passed("dead_ends table + decisions.supersedes_id present")

    print("\n--- Dead-end write guard ---")
    dec = write_memory("decision", project, {
        "what": "Chose PostgreSQL as the primary OLTP store",
        "why":  "ACID guarantees and JSONB fit our workload",
        "agent": "claude-code",
    })
    assert dec["status"] == "committed", dec
    did = dec["id"]
    _passed("seed decision committed")

    valid = write_memory("dead_end", project, {
        "what_tried": "Evaluated MongoDB for the primary store",
        "why_failed": "lack of multi-document transactions caused inventory desync",
        "chosen_decision_id": did,
        "agent": "claude-code",
    })
    assert valid["status"] == "committed", valid
    _passed("valid dead_end committed + linked")

    vague = write_memory("dead_end", project, {
        "what_tried": "tried redis",
        "why_failed": "too slow under our load profile",
    })
    assert vague["status"] == "staged", vague
    _passed("vague what_tried -> staged (guard, not bypassed)")

    missing = write_memory("dead_end", project, {"what_tried": "Considered DynamoDB for the store"})
    assert missing["status"] == "staged", missing
    _passed("missing why_failed -> staged")

    dangling = write_memory("dead_end", project, {
        "what_tried": "Considered Cassandra for the primary store",
        "why_failed": "operational overhead too high for our team size",
        "chosen_decision_id": "does-not-exist",
    })
    assert dangling["status"] == "rejected", dangling
    _passed("dangling chosen_decision_id -> rejected")

    dup = write_memory("dead_end", project, {
        "what_tried": "Evaluated MongoDB for the primary store again",
        "why_failed": "same multi-document transaction limitation as before",
    })
    assert dup["status"] == "staged", dup  # fuzzy-dup of the first dead end
    _passed("fuzzy-duplicate dead_end -> staged")

    print("\n--- Provenance round-trip ---")
    superseded = write_memory("decision", project, {
        "what": "Migrated the primary store from Postgres to CockroachDB",
        "why":  "needed multi-region active-active writes",
        "agent": "claude-code",
        "supersedes_id": did,
    })
    assert superseded["status"] == "committed", superseded

    pv = get_provenance(did)
    assert pv is not None and pv["decision"]["id"] == did
    assert any("MongoDB" in de["what_tried"] for de in pv["dead_ends"]), pv["dead_ends"]
    _passed("get_provenance returns linked dead ends")

    pv2 = get_provenance(superseded["id"])
    assert pv2["supersedes"] and pv2["supersedes"]["id"] == did
    _passed("supersession chain resolves (1 hop)")

    assert get_provenance("missing-id") is None
    _passed("provenance of unknown decision -> None")

    print("\n--- Dead ends stay out of decision recall ---")
    # `did` was superseded above, so Phase 4 auto-archives it: retrievable only
    # with include_archived=True, but never surfaced as a dead end.
    ctx = read_memory(project, query="primary store database choice", include_archived=True)
    warm_ids = {d["id"] for d in ctx["warm"]["decisions"]}
    assert did in warm_ids, "seed decision should be retrievable (archived)"
    conn = get_connection()
    de_ids = {r["id"] for r in conn.execute("SELECT id FROM dead_ends")}
    conn.close()
    assert not (warm_ids & de_ids), "dead ends leaked into warm decisions"
    _passed("read_memory warm tier excludes dead ends")

    print("\n--- Deleting a decision preserves its dead ends (ON DELETE SET NULL) ---")
    conn = get_connection()
    conn.execute("DELETE FROM decisions WHERE id=?", (did,))
    conn.commit()
    survivor = conn.execute(
        "SELECT chosen_decision_id FROM dead_ends WHERE chosen_decision_id IS NULL "
        "AND what_tried LIKE 'Evaluated MongoDB%'"
    ).fetchone()
    # the superseding decision's supersedes_id should also be NULLed, not dangling
    sup_after = conn.execute(
        "SELECT supersedes_id FROM decisions WHERE id=?", (superseded["id"],)
    ).fetchone()
    conn.close()
    assert survivor is not None, "dead end was erased when its decision was deleted"
    assert sup_after["supersedes_id"] is None, "supersedes_id left dangling after delete"
    _passed("decision delete -> dead end survives, links NULLed")

    print("\n--- Idempotent global-config init ---")
    cfg = Path(_TMP) / "CLAUDE.md"
    cfg.write_text("# Existing user rules\n\nKeep me.\n", encoding="utf-8")
    r1 = init_global_config(targets=[cfg])
    assert r1[0][1] == "written"
    body1 = cfg.read_text(encoding="utf-8")
    r2 = init_global_config(targets=[cfg])
    assert r2[0][1] == "unchanged", r2
    body2 = cfg.read_text(encoding="utf-8")
    assert body1 == body2
    assert body2.count(BEGIN) == 1 and body2.count(END) == 1, "block duplicated"
    assert "Keep me." in body2, "user content clobbered"
    _passed("hive init is idempotent (no duplicate Hive block)")

    print("\n------------------------------------------------------------")
    print("  Day 8 / Phase 3 complete — all assertions passed")
    print("------------------------------------------------------------")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
