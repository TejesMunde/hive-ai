import uuid
from datetime import datetime, timezone

from hive.db.setup import get_connection
from hive.core.guard import validate, send_to_staging
from hive.core.policy import category_of, policy_action, record_outcome
from hive.core.audit import log as audit_log


def _decision_exists(conn, decision_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM decisions WHERE id=?", (decision_id,)
    ).fetchone() is not None


def write_memory(record_type: str, project: str, data: dict) -> dict:
    """
    The only way to write anything to Hive memory.

    record_type : 'decision' | 'snapshot' | 'open_task' | 'dead_end'
    project     : project slug, e.g. 'hive-api'
    data        : dict with fields for that record type

    Returns:
      { "status": "committed"|"staged"|"auto_rejected"|"rejected",
        "id": ..., "reason": ... }

    Day 5: if guard policy for the failing category is 'auto_reject', the
    record is dropped without going through staging — learned from history.
    """

    if record_type not in ("decision", "snapshot", "open_task", "dead_end"):
        audit_log(project, "write_rejected",
                  {"type": record_type, "reason": "unknown_record_type"})
        return {"status": "rejected", "id": None,
                "reason": f"Unknown record type: '{record_type}'"}

    is_valid, reason = validate(record_type, project, data)

    if not is_valid:
        cat = category_of(reason)
        if policy_action(project, cat) == "auto_reject":
            print(f"[hive] Auto-rejected — category '{cat}' "
                  f"has been wrong every time it was reviewed for '{project}'")
            audit_log(project, "write_auto_rejected",
                      {"type": record_type, "category": cat, "reason": reason})
            return {"status": "auto_rejected", "id": None, "reason": reason}
        send_to_staging(record_type, project, data, reason)
        audit_log(project, "write_staged",
                  {"type": record_type, "category": cat, "reason": reason})
        return {"status": "staged", "id": None, "reason": reason}

    record_id = str(uuid.uuid4())
    now       = datetime.now(timezone.utc).isoformat()
    conn      = get_connection()

    try:
        # Phase 3: reject dangling decision references up front.
        sup    = (data.get("supersedes_id") or "").strip() or None
        chosen = (data.get("chosen_decision_id") or "").strip() or None
        for ref in (sup, chosen):
            if ref and not _decision_exists(conn, ref):
                audit_log(project, "write_rejected",
                          {"type": record_type, "reason": f"unknown decision ref: {ref}"})
                return {"status": "rejected", "id": None,
                        "reason": f"Referenced decision does not exist: '{ref}'"}

        if record_type == "decision":
            conn.execute(
                """INSERT INTO decisions
                   (id, project, what, why, agent, created_at, confidence, supersedes_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    record_id, project,
                    data.get("what", "").strip(),
                    data.get("why",  "").strip(),
                    data.get("agent", "unknown"),
                    now,
                    data.get("confidence", 1.0),
                    sup,
                ),
            )

        elif record_type == "snapshot":
            conn.execute(
                """INSERT INTO snapshots
                   (id, project, file_structure, active_stack, current_module, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    record_id, project,
                    data.get("file_structure",  ""),
                    data.get("active_stack",    ""),
                    data.get("current_module",  ""),
                    now,
                ),
            )

        elif record_type == "open_task":
            conn.execute(
                """INSERT INTO open_tasks
                   (id, project, description, assigned_agent, status, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    record_id, project,
                    data.get("description",    "").strip(),
                    data.get("assigned_agent", ""),
                    "open",
                    now,
                ),
            )

        elif record_type == "dead_end":
            conn.execute(
                """INSERT INTO dead_ends
                   (id, project, what_tried, why_failed, chosen_decision_id,
                    agent, created_at, confidence)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    record_id, project,
                    data.get("what_tried", "").strip(),
                    data.get("why_failed", "").strip(),
                    chosen,
                    data.get("agent", "unknown"),
                    now,
                    data.get("confidence", 1.0),
                ),
            )

        conn.commit()
        print(f"[hive] Committed {record_type} → {record_id[:8]}…")
        audit_log(project, "write_commit",
                  {"type": record_type, "id": record_id})
        return {"status": "committed", "id": record_id, "reason": "ok"}

    except Exception as e:
        conn.rollback()
        audit_log(project, "write_rejected",
                  {"type": record_type, "reason": str(e)})
        return {"status": "rejected", "id": None, "reason": str(e)}

    finally:
        conn.close()


def close_task(task_id: str) -> dict:
    """Mark an open task as done."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT project FROM open_tasks WHERE id=?", (task_id,)
        ).fetchone()
        cur = conn.execute(
            "UPDATE open_tasks SET status='done' WHERE id=? AND status='open'",
            (task_id,),
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"status": "not_found", "reason": "Task not found or already closed"}
        print(f"[hive] Task closed → {task_id[:8]}…")
        if row:
            audit_log(row["project"], "task_close", {"id": task_id})
        return {"status": "closed"}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "reason": str(e)}
    finally:
        conn.close()


def promote_from_staging(staging_id: str) -> dict:
    """
    Accept a staged record — re-runs validation with a bypass flag
    and commits it directly. Used by the staging review CLI.
    """
    import json
    conn = get_connection()
    row  = conn.execute(
        "SELECT * FROM staging WHERE id=?", (staging_id,)
    ).fetchone()
    conn.close()

    if not row:
        return {"status": "not_found", "reason": "Staging record not found"}

    data = json.loads(row["data"])

    record_id = str(uuid.uuid4())
    now       = datetime.now(timezone.utc).isoformat()
    conn      = get_connection()

    try:
        rtype   = row["type"]
        project = row["project"]

        if rtype == "decision":
            conn.execute(
                """INSERT INTO decisions
                   (id, project, what, why, agent, created_at, confidence)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    record_id, project,
                    data.get("what",  "").strip(),
                    data.get("why",   "").strip(),
                    data.get("agent", "human-reviewed"),
                    now, 1.0,
                ),
            )
        elif rtype == "snapshot":
            conn.execute(
                """INSERT INTO snapshots
                   (id, project, file_structure, active_stack, current_module, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (record_id, project,
                 data.get("file_structure", ""),
                 data.get("active_stack",   ""),
                 data.get("current_module", ""),
                 now),
            )
        elif rtype == "open_task":
            conn.execute(
                """INSERT INTO open_tasks
                   (id, project, description, assigned_agent, status, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (record_id, project,
                 data.get("description",    "").strip(),
                 data.get("assigned_agent", ""),
                 "open", now),
            )
        elif rtype == "dead_end":
            conn.execute(
                """INSERT INTO dead_ends
                   (id, project, what_tried, why_failed, chosen_decision_id,
                    agent, created_at, confidence)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (record_id, project,
                 data.get("what_tried", "").strip(),
                 data.get("why_failed", "").strip(),
                 (data.get("chosen_decision_id") or "").strip() or None,
                 data.get("agent", "human-reviewed"),
                 now, 1.0),
            )

        conn.execute("DELETE FROM staging WHERE id=?", (staging_id,))
        conn.commit()

        # Day 5: log outcome so `staging tune` can learn from it.
        record_outcome(project, rtype, row["reason"], "accepted")
        # Day 6: audit trail.
        audit_log(project, "staging_accept",
                  {"staging_id": staging_id, "id": record_id, "type": rtype})

        print(f"[hive] Promoted from staging → {record_id[:8]}…")
        return {"status": "promoted", "id": record_id}

    except Exception as e:
        conn.rollback()
        return {"status": "error", "reason": str(e)}
    finally:
        conn.close()


def reject_from_staging(staging_id: str) -> dict:
    """Permanently delete a staged record."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT project, type, reason FROM staging WHERE id=?", (staging_id,)
        ).fetchone()
        cur = conn.execute("DELETE FROM staging WHERE id=?", (staging_id,))
        conn.commit()
        if cur.rowcount == 0:
            return {"status": "not_found"}

        # Day 5: log outcome.
        if row:
            record_outcome(row["project"], row["type"], row["reason"], "rejected")
            audit_log(row["project"], "staging_reject",
                      {"staging_id": staging_id, "type": row["type"]})

        print(f"[hive] Rejected staging record {staging_id[:8]}…")
        return {"status": "rejected"}
    except Exception as e:
        conn.rollback()
        return {"status": "error", "reason": str(e)}
    finally:
        conn.close()
