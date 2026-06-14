"""
Phase 6: git-commit capture — the only impure edge of the auto-capture path.

`hive capture <sha>` is invoked by the post-commit hook. It:
  1. shells `git show -s --format=%B <sha>` to get the commit MESSAGE (not diff),
  2. feeds the text to the PURE extractor (hive.core.extract),
  3. on a Skip → logs `extract_skipped` and exits (the quality floor; nothing
     reaches staging),
  4. on a Candidate → calls the NORMAL write_memory with source='git-hook' and
     MACHINE_CONFIDENCE — the guard still runs over it like any other write,
  5. refreshes the project snapshot from the working tree.

The hook NEVER writes a handoff (delta-explosion guard) and NEVER bypasses the
guard. Machine decisions arrive at reduced confidence so they rank below confirmed
human decisions until a human reinforces them.

Usage:
    python -m hive.cli.capture <sha> [--project P] [--repo PATH]
    python -m hive.cli.capture stats [--project P]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from hive.core.extract import parse_commit, extract_decision, Candidate
from hive.core.writer import write_memory
from hive.core.audit import log as audit_log
from hive.db.setup import get_connection

# Machine writes arrive below the 1.0 human ceiling so they rank slightly lower
# until reinforced. Not auto-reinforced (avoids the cap-pressure feedback loop).
MACHINE_CONFIDENCE = 0.6
SOURCE = "git-hook"


def _run_git(args: list[str], repo: str | None) -> str:
    cmd = ["git"]
    if repo:
        cmd += ["-C", repo]
    cmd += args
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"git {' '.join(args)} failed")
    return out.stdout


def _default_project(repo: str | None) -> str:
    """Project slug defaults to the repo directory name."""
    try:
        top = _run_git(["rev-parse", "--show-toplevel"], repo).strip()
        if top:
            return Path(top).name
    except Exception:
        pass
    return Path(repo or ".").resolve().name


def _snapshot_from_tree(repo: str | None) -> dict:
    """Cheap snapshot: top-level tracked entries + the commit's changed files."""
    try:
        tracked = _run_git(["ls-tree", "--name-only", "HEAD"], repo).split()
    except Exception:
        tracked = []
    structure = ", ".join(sorted(tracked)[:40])
    return {"file_structure": structure or "(empty tree)"}


def capture_commit(sha: str, project: str | None = None, repo: str | None = None) -> dict:
    """
    Extract a decision from one commit and (if it clears the floor) write it.
    Returns a status dict. Pure-extractor decisions go through the real guard.
    """
    project = project or _default_project(repo)
    try:
        raw = _run_git(["show", "-s", "--format=%B", sha], repo)
    except Exception as e:
        return {"status": "error", "reason": str(e)}

    info = parse_commit(raw)
    result = extract_decision(info)

    if not isinstance(result, Candidate):
        # Quality floor: dropped before the guard. Observable, never staged.
        audit_log(project, "extract_skipped",
                  {"sha": sha[:12], "reason": result.reason, "subject": info.subject[:80]})
        print(f"[hive] capture: skipped {sha[:8]} ({result.reason})")
        return {"status": "skipped", "reason": result.reason}

    # Surviving candidate → normal write path. Guard still runs.
    wrote = write_memory(
        "decision", project,
        {"what": result.what, "why": result.why, "agent": SOURCE,
         "confidence": MACHINE_CONFIDENCE},
        source=SOURCE,
    )

    # Refresh the project snapshot (decisions + snapshot only — no handoff).
    snap = write_memory("snapshot", project, _snapshot_from_tree(repo), source=SOURCE)

    print(f"[hive] capture: {sha[:8]} → decision {wrote['status']}, snapshot {snap['status']}")
    return {"status": wrote["status"], "decision": wrote, "snapshot": snap["status"]}


def stats(project: str | None = None) -> dict:
    """
    Observability for the cap-pressure + machine-volume metrics the review flagged:
      - live decisions sitting at exactly confidence == 1.0 (cap saturation),
      - live decision counts by source,
      - extract_skipped counts by reason.
    """
    conn = get_connection()
    try:
        where = "WHERE archived_at IS NULL"
        params: tuple = ()
        if project:
            where += " AND project=?"
            params = (project,)

        at_cap = conn.execute(
            f"SELECT COUNT(*) AS n FROM decisions {where} AND confidence >= 1.0", params
        ).fetchone()["n"]
        live = conn.execute(
            f"SELECT COUNT(*) AS n FROM decisions {where}", params
        ).fetchone()["n"]
        by_source = conn.execute(
            f"SELECT COALESCE(source,'agent') AS s, COUNT(*) AS n "
            f"FROM decisions {where} GROUP BY s ORDER BY n DESC", params
        ).fetchall()

        # Skip reasons from the audit log.
        sk_where = "WHERE kind='extract_skipped'"
        sk_params: tuple = ()
        if project:
            sk_where += " AND project=?"
            sk_params = (project,)
        skip_rows = conn.execute(
            f"SELECT payload FROM audit_log {sk_where}", sk_params
        ).fetchall()
    finally:
        conn.close()

    import json
    skip_counts: dict[str, int] = {}
    for r in skip_rows:
        reason = json.loads(r["payload"]).get("reason", "unknown")
        skip_counts[reason] = skip_counts.get(reason, 0) + 1

    return {
        "live_decisions": live,
        "at_confidence_1.0": at_cap,
        "by_source": {r["s"]: r["n"] for r in by_source},
        "skipped": skip_counts,
    }


def calibrate(n: int = 50, repo: str | None = None) -> dict:
    """
    LOG-ONLY: run the pre-filter over the last `n` real commits and report the
    pass rate + skip-reason breakdown. Writes NOTHING — pure calibration so we can
    tell noise-filtering (healthy ~15–40% pass) from cue-too-broad (>40% = staging
    flood risk) on real history, not synthetic commits.
    """
    raw = _run_git(["log", f"-{n}", "--format=%H%x1f%B%x1e"], repo)
    records = [r for r in raw.split("\x1e") if r.strip()]
    passed = 0
    reasons: dict[str, int] = {}
    rows = []
    for rec in records:
        sha, _, msg = rec.strip().partition("\x1f")
        res = extract_decision(parse_commit(msg))
        subj = msg.strip().splitlines()[0][:55] if msg.strip() else ""
        if isinstance(res, Candidate):
            passed += 1
            rows.append(("PASS", sha[:8], subj))
        else:
            reasons[res.reason] = reasons.get(res.reason, 0) + 1
            rows.append((f"skip:{res.reason}", sha[:8], subj))
    total = len(records)
    rate = (passed / total) if total else 0.0
    verdict = ("EMPTY" if not total
               else "TOO_BROAD (staging-flood risk)" if rate > 0.40
               else "OK (filtering noise)" if rate >= 0.15
               else "TOO_NARROW (or atypical history)")
    return {"total": total, "passed": passed, "pass_rate": rate,
            "skipped": reasons, "verdict": verdict, "rows": rows}


def main() -> None:
    args = sys.argv[1:]
    project = None
    repo = None
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--project" and i + 1 < len(args):
            project = args[i + 1]; i += 2
        elif args[i] == "--repo" and i + 1 < len(args):
            repo = args[i + 1]; i += 2
        else:
            rest.append(args[i]); i += 1

    if rest and rest[0] == "stats":
        s = stats(project)
        print(f"  live decisions          : {s['live_decisions']}")
        print(f"  at confidence 1.0 (cap) : {s['at_confidence_1.0']}")
        print(f"  by source               : {s['by_source']}")
        print(f"  extract skipped         : {s['skipped']}")
        return

    if rest and rest[0] == "calibrate":
        n = int(rest[1]) if len(rest) > 1 else 50
        c = calibrate(n, repo=repo)
        print(f"  commits analyzed : {c['total']}")
        print(f"  PASS (candidate) : {c['passed']}  ({100*c['pass_rate']:.1f}%)")
        for r, k in sorted(c["skipped"].items()):
            print(f"  skip {r:<10} : {k}  ({100*k/c['total']:.1f}%)" if c['total'] else f"  skip {r}: {k}")
        print(f"  verdict          : {c['verdict']}")
        return

    if not rest:
        print("usage: python -m hive.cli.capture <sha>      [--project P] [--repo PATH]")
        print("       python -m hive.cli.capture stats      [--project P]")
        print("       python -m hive.cli.capture calibrate [N] [--repo PATH]")
        sys.exit(2)

    capture_commit(rest[0], project=project, repo=repo)


if __name__ == "__main__":
    main()
