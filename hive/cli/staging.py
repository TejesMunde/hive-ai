"""
Staging review CLI.

Usage (run from /hive_mind):
    python -m hive.cli.staging list   [--project PROJECT]
    python -m hive.cli.staging accept STAGING_ID
    python -m hive.cli.staging reject STAGING_ID
    python -m hive.cli.staging clear  --project PROJECT   # reject all staged for a project
"""

import sys
import json
import argparse

from hive.db.setup import get_connection, init_db
from hive.core.writer import promote_from_staging, reject_from_staging
from hive.core.policy import stats as policy_stats, tune_policies

# ── ANSI colours (degrade gracefully if unsupported) ─────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
YELLOW = "\033[33m"
GREEN  = "\033[32m"
RED    = "\033[31m"
CYAN   = "\033[36m"


def _c(text, *codes):
    return "".join(codes) + str(text) + RESET


def cmd_list(project: str | None):
    conn = get_connection()
    query = "SELECT * FROM staging ORDER BY created_at ASC"
    params = ()
    if project:
        query  = "SELECT * FROM staging WHERE project=? ORDER BY created_at ASC"
        params = (project,)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print(_c("  No staged records.", DIM))
        return

    print(_c(f"\n  {len(rows)} staged record(s)\n", BOLD))

    for i, row in enumerate(rows, 1):
        data = json.loads(row["data"])

        # Main field preview
        preview = (
            data.get("what") or
            data.get("description") or
            data.get("file_structure") or
            "(no preview)"
        )[:80]

        print(_c(f"  [{i}] ", BOLD) + _c(row["id"][:8] + "…", DIM))
        print(f"      type    : {_c(row['type'], CYAN)}")
        print(f"      project : {row['project']}")
        print(f"      preview : {preview}")
        print(f"      reason  : {_c(row['reason'], YELLOW)}")
        print(f"      staged  : {row['created_at'][:19]}")

        if row["type"] == "decision":
            why = data.get("why", "")
            if why:
                print(f"      why     : {why[:80]}")

        print()

    print(_c("  Commands:", DIM))
    print(_c("    python -m hive.cli.staging accept <id-prefix>", DIM))
    print(_c("    python -m hive.cli.staging reject <id-prefix>\n", DIM))


def _resolve_id(prefix: str) -> str | None:
    """Match a short prefix to a full staging ID."""
    conn = get_connection()
    rows = conn.execute("SELECT id FROM staging").fetchall()
    conn.close()

    matches = [r["id"] for r in rows if r["id"].startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(_c(f"  Ambiguous prefix '{prefix}' — matches {len(matches)} records. Use more characters.", RED))
        return None
    print(_c(f"  No staging record found with prefix '{prefix}'.", RED))
    return None


def cmd_accept(id_prefix: str):
    full_id = _resolve_id(id_prefix)
    if not full_id:
        return

    result = promote_from_staging(full_id)
    if result["status"] == "promoted":
        print(_c(f"  ✓ Accepted and committed → {result['id'][:8]}…", GREEN))
    else:
        print(_c(f"  ✗ Failed: {result.get('reason', 'unknown error')}", RED))


def cmd_reject(id_prefix: str):
    full_id = _resolve_id(id_prefix)
    if not full_id:
        return

    result = reject_from_staging(full_id)
    if result["status"] == "rejected":
        print(_c(f"  ✓ Rejected and removed from staging.", GREEN))
    else:
        print(_c(f"  ✗ Failed: {result.get('reason', 'unknown error')}", RED))


def cmd_clear(project: str):
    conn = get_connection()
    rows = conn.execute(
        "SELECT id FROM staging WHERE project=?", (project,)
    ).fetchall()
    conn.close()

    if not rows:
        print(_c(f"  No staged records for project '{project}'.", DIM))
        return

    confirm = input(
        _c(f"  Reject all {len(rows)} staged records for '{project}'? [y/N] ", YELLOW)
    )
    if confirm.strip().lower() != "y":
        print(_c("  Aborted.", DIM))
        return

    for row in rows:
        reject_from_staging(row["id"])

    print(_c(f"  ✓ Cleared {len(rows)} staged record(s) for '{project}'.", GREEN))


# ── Day 5: stats + tune + review ─────────────────────────────────────────────

def cmd_stats(project: str | None):
    rows = policy_stats(project)
    if not rows:
        print(_c("  No staging history yet — nothing to analyse.", DIM))
        return

    print(_c(f"\n  Guard category history ({len(rows)} rows)\n", BOLD))
    print(f"  {'project':<18} {'category':<36} {'n':>4} {'acc':>4} {'rej':>4} {'rate':>6}  action")
    print(f"  {'-'*18} {'-'*36} {'-'*4} {'-'*4} {'-'*4} {'-'*6}  {'-'*12}")
    for r in rows:
        color = RED if r["action"] == "auto_reject" else GREEN
        rate_str = f"{r['accept_rate']*100:5.1f}%"
        print(
            f"  {r['project'][:18]:<18} {r['category'][:36]:<36} "
            f"{r['samples']:>4} {r['accepted']:>4} {r['rejected']:>4} "
            f"{rate_str:>6}  {_c(r['action'], color)}"
        )
    print()


def cmd_tune(project: str | None):
    summary = tune_policies(project)
    if not summary:
        print(_c("  No history rows yet — staging tune is a no-op.", DIM))
        return

    print(_c(f"\n  Tuned {len(summary)} (project, category) rows\n", BOLD))
    changed = 0
    for r in summary:
        line = (f"  {r['project'][:14]:<14}  {r['category'][:46]:<46}  "
                f"({r['samples']} samples, {r['accept_rate']*100:.0f}% accepted)")
        if r["action"] == "auto_reject":
            changed += 1
            print(_c("  AUTO-REJECT" + line[2:], RED))
        else:
            print(_c("  stage      " + line[2:], DIM))
    print()
    print(_c(f"  {changed} (project, category) row(s) now set to auto_reject.", BOLD))


def cmd_review(project: str | None):
    """Interactive walk-through of pending staged records."""
    conn = get_connection()
    query = "SELECT * FROM staging ORDER BY created_at ASC"
    params = ()
    if project:
        query  = "SELECT * FROM staging WHERE project=? ORDER BY created_at ASC"
        params = (project,)
    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print(_c("  Nothing in staging. Inbox zero.", GREEN))
        return

    print(_c(f"\n  {len(rows)} record(s) to review. [y=accept  n=reject  s=skip  q=quit]\n", BOLD))

    for i, row in enumerate(rows, 1):
        data = json.loads(row["data"])
        preview = (
            data.get("what") or data.get("description") or data.get("file_structure") or "(no preview)"
        )[:90]

        print(_c(f"  [{i}/{len(rows)}] {row['id'][:8]}  type={row['type']}  project={row['project']}", BOLD))
        print(f"        preview : {preview}")
        print(f"        reason  : {_c(row['reason'], YELLOW)}")
        if row["type"] == "decision" and data.get("why"):
            print(f"        why     : {data['why'][:90]}")

        ans = input(_c("        y/n/s/q ? ", CYAN)).strip().lower()
        if ans == "y":
            r = promote_from_staging(row["id"])
            print(_c(f"          → {r['status']}", GREEN))
        elif ans == "n":
            r = reject_from_staging(row["id"])
            print(_c(f"          → {r['status']}", RED))
        elif ans == "q":
            print(_c("  Quit.", DIM))
            return
        else:
            print(_c("          → skipped", DIM))
        print()


def main():
    init_db()

    parser = argparse.ArgumentParser(
        prog="python -m hive.cli.staging",
        description="Hive Mind — staging review CLI",
    )
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="Show all staged records")
    p_list.add_argument("--project", default=None, help="Filter by project slug")

    p_accept = sub.add_parser("accept", help="Accept a staged record into memory")
    p_accept.add_argument("id", help="Staging ID or prefix")

    p_reject = sub.add_parser("reject", help="Permanently reject a staged record")
    p_reject.add_argument("id", help="Staging ID or prefix")

    p_clear = sub.add_parser("clear", help="Reject all staged records for a project")
    p_clear.add_argument("--project", required=True, help="Project slug")

    p_stats = sub.add_parser("stats", help="Per-category accept/reject history + policy")
    p_stats.add_argument("--project", default=None, help="Filter by project slug")

    p_tune  = sub.add_parser("tune",  help="Recompute auto-reject policy from history")
    p_tune.add_argument("--project",  default=None, help="Tune only this project")

    p_review = sub.add_parser("review", help="Interactive review walk-through")
    p_review.add_argument("--project", default=None, help="Filter by project slug")

    args = parser.parse_args()

    if   args.command == "list":   cmd_list(args.project)
    elif args.command == "accept": cmd_accept(args.id)
    elif args.command == "reject": cmd_reject(args.id)
    elif args.command == "clear":  cmd_clear(args.project)
    elif args.command == "stats":  cmd_stats(args.project)
    elif args.command == "tune":   cmd_tune(args.project)
    elif args.command == "review": cmd_review(args.project)
    else: parser.print_help()


if __name__ == "__main__":
    main()
