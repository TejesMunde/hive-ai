"""
Phase 5: agent handoff packets.

A handoff packet is what one agent hands the next: the current project state plus
a *delta of what changed since the previous handoff*. Packets are persisted in
the `handoffs` table so each new packet's delta boundary is the prior packet's
`created_at` — that delta is the continuity payload that makes a handoff more than
a plain read_memory.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from hive.db.setup import get_connection
from hive.core.audit import log as audit_log
from hive.core.reader import read_memory

TOP_DECISIONS = 5


def _latest_handoff_ts(conn, project: str) -> str | None:
    row = conn.execute(
        "SELECT created_at FROM handoffs WHERE project=? ORDER BY created_at DESC LIMIT 1",
        (project,),
    ).fetchone()
    return row["created_at"] if row else None


def _delta(conn, project: str, since: str | None) -> dict:
    """
    What changed since `since` (a prior handoff's created_at). When since is None
    (first ever handoff) everything is returned, so a cold-start agent still gets
    the full picture. Archived/superseded decisions ARE included here — they are
    part of "what changed".
    """
    where_created = "AND created_at > ?" if since else ""
    args = (project, since) if since else (project,)

    decisions = conn.execute(
        "SELECT id, what, why, agent, created_at, archived_at, supersedes_id "
        f"FROM decisions WHERE project=? {where_created} ORDER BY created_at ASC",
        args,
    ).fetchall()
    dead_ends = conn.execute(
        "SELECT id, what_tried, why_failed, chosen_decision_id, agent, created_at "
        f"FROM dead_ends WHERE project=? {where_created} ORDER BY created_at ASC",
        args,
    ).fetchall()
    tasks_opened = conn.execute(
        "SELECT id, description, assigned_agent, created_at "
        f"FROM open_tasks WHERE project=? {where_created} ORDER BY created_at ASC",
        args,
    ).fetchall()

    # Tasks closed within the interval (needs closed_at, added in Phase 5).
    where_closed = "AND closed_at > ?" if since else "AND closed_at IS NOT NULL"
    closed = conn.execute(
        "SELECT id, description, assigned_agent, closed_at "
        f"FROM open_tasks WHERE project=? AND status='done' {where_closed} "
        "ORDER BY closed_at ASC",
        args,
    ).fetchall()

    return {
        "since":            since,
        "decisions_added":  [dict(r) for r in decisions],
        "dead_ends_added":  [dict(r) for r in dead_ends],
        "tasks_opened":     [dict(r) for r in tasks_opened],
        "tasks_closed":     [dict(r) for r in closed],
    }


def create_handoff(project: str, from_agent: str | None = None,
                   to_agent: str | None = None) -> dict:
    """
    Build, persist, and return a handoff packet: current state + delta since the
    previous handoff for this project.
    """
    now = datetime.now(timezone.utc).isoformat()
    handoff_id = str(uuid.uuid4())

    # State: reuse read_memory so the warm-tier rules (decay, archived-excluded)
    # are exactly the production ones. No query → recency-ranked live decisions.
    ctx = read_memory(project, query="")
    state = {
        "open_tasks":      ctx["hot"]["open_tasks"],
        "latest_snapshot": ctx["hot"]["latest_snapshot"],
        "top_decisions":   ctx["warm"]["decisions"][:TOP_DECISIONS],
    }

    conn = get_connection()
    try:
        since = _latest_handoff_ts(conn, project)
        delta = _delta(conn, project, since)

        packet = {
            "id":         handoff_id,
            "project":    project,
            "from_agent": from_agent,
            "to_agent":   to_agent,
            "created_at": now,
            "state":      state,
            "delta":      delta,
        }
        conn.execute(
            "INSERT INTO handoffs (id, project, from_agent, to_agent, payload, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (handoff_id, project, from_agent, to_agent, json.dumps(packet), now),
        )
        conn.commit()
    finally:
        conn.close()

    audit_log(project, "handoff_create", {
        "id": handoff_id, "from": from_agent, "to": to_agent,
        "decisions_added": len(delta["decisions_added"]),
        "tasks_closed":    len(delta["tasks_closed"]),
    })
    print(f"[hive] Handoff {handoff_id[:8]}… {from_agent or '?'} → {to_agent or '?'}")
    return packet


def get_handoff(handoff_id: str) -> dict | None:
    """Read a persisted handoff packet back by id."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT payload FROM handoffs WHERE id=?", (handoff_id,)
        ).fetchone()
        return json.loads(row["payload"]) if row else None
    finally:
        conn.close()


def latest_handoff(project: str) -> dict | None:
    """Most recent handoff packet for a project, or None."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT payload FROM handoffs WHERE project=? ORDER BY created_at DESC LIMIT 1",
            (project,),
        ).fetchone()
        return json.loads(row["payload"]) if row else None
    finally:
        conn.close()
