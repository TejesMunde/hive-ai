"""
Unified `hive` command — the single console entry point for the packaged tool.

Routes the first argument to a subcommand. The admin subcommands delegate to the
existing per-module `main()`s (each parses `sys.argv[1:]` itself), so this is a thin
dispatcher with no logic duplication — it rewrites `sys.argv` and hands off. The two
`recall` / `remember` subcommands are small wrappers over the public API so a freshly
installed CLI can actually read and write memory, not just run admin tasks.

Installed as the `hive` console script (see pyproject.toml). Also runnable as
`python -m hive.cli.main`.
"""

from __future__ import annotations

import json
import sys

_DELEGATES = {
    "init":    "hive.cli.init",
    "capture": "hive.cli.capture",
    "hook":    "hive.cli.hook",
    "staging": "hive.cli.staging",
    "audit":   "hive.cli.audit",
}

_USAGE = """\
hive — persistent cross-agent memory

usage: hive <command> [args]

memory:
  recall   <project> <query...>          retrieve ranked context (JSON)
  remember <project> "<what>" "<why>"    record a decision (through the guard)

automation & review:
  capture  <sha> | stats | calibrate [N] extract decisions from git commits
  hook     install | uninstall | status  manage the post-commit capture hook
  staging  list | accept | reject | …    review guard-flagged writes
  audit    tail | counts | fails         inspect the append-only event log
  init                                   inject Hive usage block into agent configs

  hive --version    show version
  hive --help       show this message
"""


def _delegate(module_name: str, sub: str, rest: list[str]) -> int:
    """Run an existing CLI module's main() with a rewritten argv."""
    import importlib
    mod = importlib.import_module(module_name)
    sys.argv = [f"hive-{sub}", *rest]
    rc = mod.main()
    return int(rc) if isinstance(rc, int) else 0


def _cmd_recall(rest: list[str]) -> int:
    if len(rest) < 2:
        print("usage: hive recall <project> <query...>", file=sys.stderr)
        return 2
    from hive import read_memory
    project, query = rest[0], " ".join(rest[1:])
    ctx = read_memory(project, query=query)
    print(json.dumps(ctx, indent=2, default=str))
    return 0


def _cmd_remember(rest: list[str]) -> int:
    if len(rest) < 3:
        print('usage: hive remember <project> "<what>" "<why>" [agent]', file=sys.stderr)
        return 2
    from hive import write_memory
    project, what, why = rest[0], rest[1], rest[2]
    agent = rest[3] if len(rest) > 3 else "hive-cli"
    result = write_memory("decision", project,
                          {"what": what, "why": why, "agent": agent})
    print(json.dumps(result, indent=2, default=str))
    # Non-zero exit if the guard didn't commit, so scripts can detect it.
    return 0 if result.get("status") == "committed" else 1


def main() -> int:
    argv = sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0

    if argv[0] in ("-V", "--version", "version"):
        from hive import __version__
        print(f"hive {__version__}")
        return 0

    cmd, rest = argv[0], argv[1:]

    if cmd == "recall":
        return _cmd_recall(rest)
    if cmd == "remember":
        return _cmd_remember(rest)
    if cmd in _DELEGATES:
        return _delegate(_DELEGATES[cmd], cmd, rest)

    print(f"hive: unknown command '{cmd}'\n", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
