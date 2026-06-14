"""Hive Mind public API."""

from hive.db.setup import init_db, get_connection
from hive.core.writer import (
    write_memory, close_task, promote_from_staging, reject_from_staging,
    reinforce_decision, archive_decision, unarchive_decision, sweep_archive,
)
from hive.core.reader import read_memory, get_provenance
from hive.core.handoff import create_handoff, get_handoff, latest_handoff
from hive.core.routing import route_task

__all__ = [
    "init_db",
    "get_connection",
    "write_memory",
    "read_memory",
    "get_provenance",
    "close_task",
    "promote_from_staging",
    "reject_from_staging",
    "reinforce_decision",
    "archive_decision",
    "unarchive_decision",
    "sweep_archive",
    "create_handoff",
    "get_handoff",
    "latest_handoff",
    "route_task",
    "capture_commit",
]


def __getattr__(name):
    # Lazy export (PEP 562): hive.cli.capture is runnable via `python -m`, so
    # importing it eagerly here would double-load it and emit a RuntimeWarning on
    # every `-m hive.cli.capture` invocation (the hook runs that on every commit).
    if name == "capture_commit":
        from hive.cli.capture import capture_commit
        return capture_commit
    raise AttributeError(f"module 'hive' has no attribute '{name}'")
