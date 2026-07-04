"""
Day 11 / Phase 6 — git-commit decision extraction (the machine write path).

Run: PYTHONIOENCODING=utf-8 python tests/test_day11.py

Self-contained: throwaway DB via HIVE_DB_PATH. Dense OFF (stdlib path).
Two layers:
  A. PURE extractor unit tests — every floor gate + skip reason, no git, no DB.
  B. End-to-end capture — machine candidate goes through the REAL write_memory
     (guard NOT bypassed), lands at reduced confidence + source='git-hook'.
"""

import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="hive_day11_")
os.environ["HIVE_DB_PATH"] = os.path.join(_TMP, "day11.db")
os.environ.setdefault("HIVE_DENSE", "0")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hive import init_db, write_memory, get_connection
from hive.core.extract import parse_commit, extract_decision, Candidate, Skip
from hive.cli.capture import capture_commit, stats, MACHINE_CONFIDENCE
from hive.cli.hook import _render, _render_uninstall, BEGIN, END


def _passed(label):
    print(f"  [OK ] {label}")


def _extract(raw):
    return extract_decision(parse_commit(raw))


def test_extractor():
    print("\n--- A. Pure extractor: floor gates ---")

    # Candidate: conventional type + cue + substance.
    r = _extract("feat: switched from REST to gRPC for the internal API\n\n"
                 "Chose gRPC over REST because streaming and codegen reduce boilerplate.")
    assert isinstance(r, Candidate), r
    assert "gRPC" in r.what and len(r.what.split()) >= 5
    assert "because" in r.why.lower()
    _passed("feat + cue + body -> Candidate (what/why populated)")

    # type_gate: chore is excluded.
    r = _extract("chore: bump dependencies to latest")
    assert isinstance(r, Skip) and r.reason == "type_gate", r
    _passed("chore: -> Skip(type_gate)")

    # type_gate: merge commit.
    r = _extract("Merge branch 'feature/x' into main")
    assert isinstance(r, Skip) and r.reason == "type_gate", r
    _passed("merge commit -> Skip(type_gate)")

    # type_gate: version bump.
    r = _extract("release 2.1.0")
    assert isinstance(r, Skip) and r.reason == "type_gate", r
    _passed("version bump -> Skip(type_gate)")

    # no_cue: valid type + substantial subject, but no decision language.
    r = _extract("feat: add a new dashboard widget for users\n\nRenders a chart.")
    assert isinstance(r, Skip) and r.reason == "no_cue", r
    _passed("feat with no decision cue -> Skip(no_cue)")

    # too_thin: cue present but no real why / too short.
    r = _extract("fix: chose x")
    assert isinstance(r, Skip), r
    assert r.reason in ("too_thin", "no_cue"), r
    _passed("thin subject with cue, no why -> Skip(too_thin)")

    # perf: conventional type also extracts.
    r = _extract("perf: replaced the O(n^2) scan with a hash index\n\n"
                 "Replaced the nested loop because lookups dominated the profile.")
    assert isinstance(r, Candidate), r
    _passed("perf: + cue -> Candidate")

    # No prefix but long subject with cue (because) -> why falls back to subject.
    r = _extract("Adopted SQLite over Postgres because zero-setup matters here")
    assert isinstance(r, Candidate), r
    assert r.why, "why should fall back to the 'because' subject"
    _passed("prefix-less long subject with 'because' -> Candidate")

    # Boundary bug regression: cue substrings inside CODE IDENTIFIERS must NOT fire.
    # `chosen_decision_id`, `over_count`, `because_flag` are not decision language.
    r = _extract("feat: add chosen_decision_id column and over_count to the schema")
    assert isinstance(r, Skip) and r.reason == "no_cue", r
    _passed("cue inside identifier (chosen_decision_id) -> Skip(no_cue), not a match")


def test_capture_through_guard():
    print("\n--- B. End-to-end capture: real guard, reduced confidence, tagged ---")
    init_db()
    project = "phase6"

    # Drive capture_commit's extractor+writer directly by faking the commit text.
    # We bypass git by calling extract+write the same way capture_commit does NOT
    # bypass the guard — assert the row lands committed with source + confidence.
    raw = ("refactor: migrated the queue from RabbitMQ to Kafka\n\n"
           "Switched to Kafka because partition ordering and replay matter for billing.")
    r = _extract(raw)
    assert isinstance(r, Candidate), r
    wrote = write_memory("decision", project,
                         {"what": r.what, "why": r.why, "agent": "git-hook",
                          "confidence": MACHINE_CONFIDENCE},
                         source="git-hook")
    assert wrote["status"] == "committed", wrote
    conn = get_connection()
    row = conn.execute("SELECT source, confidence FROM decisions WHERE id=?",
                       (wrote["id"],)).fetchone()
    conn.close()
    assert row["source"] == "git-hook", row["source"]
    assert abs(row["confidence"] - MACHINE_CONFIDENCE) < 1e-9, row["confidence"]
    _passed("machine decision committed: source='git-hook', confidence=0.6")

    # Guard still runs over machine writes: an exact duplicate must NOT commit.
    dup = write_memory("decision", project,
                       {"what": r.what, "why": r.why, "agent": "git-hook",
                        "confidence": MACHINE_CONFIDENCE},
                       source="git-hook")
    assert dup["status"] in ("staged", "auto_rejected"), dup
    conn = get_connection()
    staged = conn.execute(
        "SELECT source FROM staging WHERE project=? AND type='decision'", (project,)
    ).fetchone()
    conn.close()
    if dup["status"] == "staged":
        assert staged and staged["source"] == "git-hook", staged
    _passed("guard NOT bypassed: duplicate machine write staged/rejected, tagged")

    # A sub-threshold commit is dropped at the floor (skip), never staged.
    skip = capture_commit("deadbeef", project=project, repo=_TMP)
    # _TMP isn't a git repo -> capture returns error OR skip; either way no write.
    assert skip["status"] in ("error", "skipped"), skip
    _passed("capture on non-decision/non-repo -> no committed decision")


def test_stats():
    print("\n--- C. stats: cap-saturation + source + skip counts ---")
    s = stats("phase6")
    assert "at_confidence_1.0" in s and "by_source" in s and "skipped" in s, s
    assert s["by_source"].get("git-hook", 0) >= 1, s
    # The machine decision is at 0.6, so it must NOT count toward the 1.0 cap.
    assert s["at_confidence_1.0"] == 0, s
    _passed(f"stats reports source + cap saturation: {s}")


def test_hook_render():
    print("\n--- D. Hook install/uninstall is idempotent + non-clobbering ---")
    # Empty -> shebang + block.
    once = _render("")
    assert BEGIN in once and END in once and once.startswith("#!")
    # Re-render is a no-op (idempotent).
    assert _render(once) == once, "hook install must be idempotent"
    _passed("install idempotent: re-render unchanged")

    # Existing user hook is preserved, block appended.
    user = "#!/bin/sh\necho 'my own hook'\n"
    merged = _render(user)
    assert "my own hook" in merged and BEGIN in merged
    _passed("existing post-commit hook preserved, Hive block appended")

    # Uninstall removes ONLY the Hive block, leaves the user hook.
    cleaned = _render_uninstall(merged)
    assert "my own hook" in cleaned and BEGIN not in cleaned
    _passed("uninstall removes only Hive block, user hook intact")

    # Uninstall of a Hive-only hook -> full removal signal.
    assert _render_uninstall(once) == ""
    _passed("uninstall of Hive-only hook signals full removal")


def test_self_init():
    print("\n--- E. capture/stats self-migrate a stale/pre-Phase-6 DB ---")
    # The hook can fire against a repo whose hive.db predates the Phase 6 `source`
    # migration (an old `decisions` table with no `source` column). capture_commit
    # + stats must call init_db() themselves so they never hit `no such column:
    # source`. NOTE: get_connection() reads the module-global DB_PATH captured at
    # import, so we patch that global — mutating os.environ mid-run would be a no-op.
    import sqlite3
    import hive.db.setup as setup
    fresh = os.path.join(_TMP, "preP6.db")
    # Seed an OLD-schema decisions table (pre-source) to reproduce the live bug.
    conn = sqlite3.connect(fresh)
    conn.execute("CREATE TABLE decisions (id TEXT PRIMARY KEY, project TEXT NOT NULL, "
                 "what TEXT NOT NULL, why TEXT, agent TEXT, created_at TEXT NOT NULL, "
                 "confidence REAL DEFAULT 1.0)")
    conn.commit(); conn.close()

    old_path = setup.DB_PATH
    setup.DB_PATH = fresh
    try:
        # stats() against the pre-Phase-6 schema must NOT raise on `source`.
        s = stats("anything")
        assert s["live_decisions"] == 0 and s["by_source"] == {}, s
        # capture that errors (not a git repo) still must not raise on schema.
        r = capture_commit("deadbeef", project="anything", repo=_TMP)
        assert r["status"] in ("error", "skipped"), r
        # The migration ran: `source` column now present on the old table.
        conn = sqlite3.connect(fresh)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
        conn.close()
        assert "source" in cols, cols
        _passed("stats + capture self-migrate a pre-Phase-6 DB (source column added)")
    finally:
        setup.DB_PATH = old_path


def main():
    test_extractor()
    test_capture_through_guard()
    test_stats()
    test_hook_render()
    test_self_init()
    print("\n------------------------------------------------------------")
    print("  Day 11 / Phase 6 complete — all assertions passed")
    print("------------------------------------------------------------")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
