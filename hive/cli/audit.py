"""
Audit log CLI.

Usage (from project root):
    python -m hive.cli.audit tail   [--project P] [--limit N]
    python -m hive.cli.audit counts [--project P]
    python -m hive.cli.audit fails  [--project P]
"""

import argparse
import json

from hive.db.setup import init_db
from hive.core.audit import tail, counts, fails

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
YELLOW = "\033[33m"
GREEN  = "\033[32m"
RED    = "\033[31m"
CYAN   = "\033[36m"

KIND_COLOR = {
    "write_commit":         GREEN,
    "write_staged":         YELLOW,
    "write_auto_rejected":  RED,
    "write_rejected":       RED,
    "staging_accept":       GREEN,
    "staging_reject":       RED,
    "task_close":           CYAN,
    "query":                DIM,
}


def _c(text, *codes):
    return "".join(codes) + str(text) + RESET


def cmd_tail(project, limit):
    rows = tail(project, limit)
    if not rows:
        print(_c("  No audit rows.", DIM))
        return

    print(_c(f"\n  Last {len(rows)} events"
             f"{(' for ' + project) if project else ''}\n", BOLD))
    for r in rows:
        color = KIND_COLOR.get(r["kind"], DIM)
        kind  = _c(f"{r['kind']:<22}", color)
        head  = f"  {r['created_at'][11:19]}  {kind}  {r['project'][:14]:<14}"
        body  = json.dumps(r["payload"])[:90]
        print(f"{head}  {body}")
    print()


def cmd_counts(project):
    c = counts(project)
    if not c:
        print(_c("  No audit rows.", DIM))
        return
    total = sum(c.values())
    print(_c(f"\n  Event counts"
             f"{(' for ' + project) if project else ''}  (total {total})\n", BOLD))
    for kind in sorted(c, key=lambda k: -c[k]):
        color = KIND_COLOR.get(kind, DIM)
        label = _c(f"{kind:<22}", color)
        print(f"  {label} {c[kind]:>6}")
    print()


def cmd_fails(project):
    rows = fails(project)
    if not rows:
        print(_c("  No failed writes — clean slate.", GREEN))
        return
    print(_c(f"\n  {len(rows)} failure(s)"
             f"{(' for ' + project) if project else ''}\n", BOLD))
    for r in rows:
        color = KIND_COLOR.get(r["kind"], RED)
        cat   = r["payload"].get("category") or r["payload"].get("reason", "")
        kind  = _c(f"{r['kind']:<22}", color)
        print(f"  {r['created_at'][11:19]}  {kind}  "
              f"{r['project'][:14]:<14}  {cat[:60]}")
    print()


def main():
    init_db()
    p = argparse.ArgumentParser(
        prog="python -m hive.cli.audit",
        description="Hive Mind — audit log CLI",
    )
    sub = p.add_subparsers(dest="command")

    p_tail = sub.add_parser("tail", help="Show recent events")
    p_tail.add_argument("--project", default=None)
    p_tail.add_argument("--limit",   type=int, default=50)

    p_counts = sub.add_parser("counts", help="Event counts by kind")
    p_counts.add_argument("--project", default=None)

    p_fails = sub.add_parser("fails", help="Show every non-commit write outcome")
    p_fails.add_argument("--project", default=None)

    args = p.parse_args()

    if   args.command == "tail":   cmd_tail(args.project, args.limit)
    elif args.command == "counts": cmd_counts(args.project)
    elif args.command == "fails":  cmd_fails(args.project)
    else:                          p.print_help()


if __name__ == "__main__":
    main()
