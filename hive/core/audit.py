"""
Day 6: append-only event log so we can soak Hive across real agent work
and replay everything that happened.

Every write outcome and every query lands here. Aggregate views drive the
Phase 1 milestone check ("agent reads correctly without touching the codebase").
"""

import json
from datetime import datetime, timezone

from hive.db.setup import get_connection


def log(project: str, kind: str, payload: dict) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO audit_log (project, kind, payload, created_at) "
            "VALUES (?,?,?,?)",
            (project, kind, json.dumps(payload),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def tail(project: str | None = None, limit: int = 50) -> list[dict]:
    conn = get_connection()
    try:
        if project:
            rows = conn.execute(
                "SELECT id, project, kind, payload, created_at "
                "FROM audit_log WHERE project=? "
                "ORDER BY id DESC LIMIT ?",
                (project, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, project, kind, payload, created_at "
                "FROM audit_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id":         r["id"],
                "project":    r["project"],
                "kind":       r["kind"],
                "payload":    json.loads(r["payload"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def counts(project: str | None = None) -> dict[str, int]:
    """kind → count, useful for the soak summary."""
    conn = get_connection()
    try:
        if project:
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM audit_log "
                "WHERE project=? GROUP BY kind", (project,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM audit_log GROUP BY kind"
            ).fetchall()
        return {r["kind"]: r["n"] for r in rows}
    finally:
        conn.close()


def fails(project: str | None = None) -> list[dict]:
    """All non-commit write outcomes — soaks show these as the failure surface."""
    fail_kinds = ("write_staged", "write_auto_rejected", "write_rejected")
    conn = get_connection()
    try:
        if project:
            rows = conn.execute(
                "SELECT id, project, kind, payload, created_at "
                "FROM audit_log "
                "WHERE project=? AND kind IN ('write_staged','write_auto_rejected','write_rejected') "
                "ORDER BY id DESC",
                (project,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, project, kind, payload, created_at "
                "FROM audit_log "
                "WHERE kind IN ('write_staged','write_auto_rejected','write_rejected') "
                "ORDER BY id DESC"
            ).fetchall()
        return [
            {
                "id":         r["id"],
                "project":    r["project"],
                "kind":       r["kind"],
                "payload":    json.loads(r["payload"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()
