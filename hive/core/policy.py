"""
Day 5: guard-rule policy + outcome learning, scoped per-project.

Categories are extracted from guard reasons by trimming everything after
the first ':', '—' or '(' — so dynamic content (the offending record's
text, similarity %, etc.) is dropped.

    "Missing or empty required field: 'what'"            → "Missing or empty required field"
    "Too vague — 'what' needs at least 5 words (got 2)"  → "Too vague"
    "Too similar to existing decision (50% match): '...'" → "Too similar to existing decision"

Policy is per-project: one project's reviewer history never alters guard
behaviour in another project.
"""

import re
import uuid
from datetime import datetime, timezone

from hive.db.setup import get_connection

MIN_SAMPLES    = 5      # need this many history rows before tuning a category
AUTO_REJECT_AT = 0.10   # accept rate at or below this → auto_reject


_CATEGORY_SPLIT = re.compile(r"[:—\(]")


def category_of(reason: str) -> str:
    head = _CATEGORY_SPLIT.split(reason, 1)[0]
    return head.strip()[:80]


def record_outcome(project: str, record_type: str, reason: str, outcome: str) -> None:
    """Append a row to staging_history. outcome ∈ {'accepted','rejected'}."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO staging_history "
            "(id, project, type, category, outcome, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                project,
                record_type,
                category_of(reason),
                outcome,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def policy_action(project: str, category: str) -> str:
    """Return 'stage' (default) or 'auto_reject' for a project+category pair."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT action FROM guard_policy WHERE project=? AND category=?",
            (project, category),
        ).fetchone()
    finally:
        conn.close()
    return row["action"] if row else "stage"


def tune_policies(project: str | None = None) -> list[dict]:
    """
    Recompute guard_policy from staging_history.

    project=None → tune every project that has history.
    project=str  → tune only that project's categories.
    """
    conn = get_connection()
    try:
        if project is None:
            rows = conn.execute(
                "SELECT project, category, "
                "       COUNT(*)                                            AS n, "
                "       SUM(CASE WHEN outcome='accepted' THEN 1 ELSE 0 END) AS accepted "
                "FROM staging_history "
                "GROUP BY project, category"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT project, category, "
                "       COUNT(*)                                            AS n, "
                "       SUM(CASE WHEN outcome='accepted' THEN 1 ELSE 0 END) AS accepted "
                "FROM staging_history "
                "WHERE project=? "
                "GROUP BY project, category",
                (project,),
            ).fetchall()

        summary = []
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            n        = row["n"]
            accepted = row["accepted"] or 0
            rate     = accepted / n if n else 0.0
            action   = ("auto_reject"
                        if n >= MIN_SAMPLES and rate <= AUTO_REJECT_AT
                        else "stage")

            conn.execute(
                "INSERT INTO guard_policy "
                "(project, category, action, sample_size, accept_rate, updated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(project, category) DO UPDATE SET "
                "  action=excluded.action, "
                "  sample_size=excluded.sample_size, "
                "  accept_rate=excluded.accept_rate, "
                "  updated_at=excluded.updated_at",
                (row["project"], row["category"], action, n, rate, now),
            )
            summary.append({
                "project":     row["project"],
                "category":    row["category"],
                "samples":     n,
                "accepted":    accepted,
                "rejected":    n - accepted,
                "accept_rate": rate,
                "action":      action,
            })

        conn.commit()
        return summary
    finally:
        conn.close()


def stats(project: str | None = None) -> list[dict]:
    """Per-(project,category) snapshot — joined history + current policy."""
    conn = get_connection()
    try:
        if project is None:
            rows = conn.execute(
                "SELECT project, category, "
                "       COUNT(*)                                            AS n, "
                "       SUM(CASE WHEN outcome='accepted' THEN 1 ELSE 0 END) AS accepted "
                "FROM staging_history "
                "GROUP BY project, category "
                "ORDER BY n DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT project, category, "
                "       COUNT(*)                                            AS n, "
                "       SUM(CASE WHEN outcome='accepted' THEN 1 ELSE 0 END) AS accepted "
                "FROM staging_history "
                "WHERE project=? "
                "GROUP BY project, category "
                "ORDER BY n DESC",
                (project,),
            ).fetchall()

        pol_rows = conn.execute(
            "SELECT project, category, action FROM guard_policy"
        ).fetchall()
        policies = {(p["project"], p["category"]): p["action"] for p in pol_rows}

        out = []
        for row in rows:
            n        = row["n"]
            accepted = row["accepted"] or 0
            out.append({
                "project":     row["project"],
                "category":    row["category"],
                "samples":     n,
                "accepted":    accepted,
                "rejected":    n - accepted,
                "accept_rate": (accepted / n) if n else 0.0,
                "action":      policies.get((row["project"], row["category"]), "stage"),
            })
        return out
    finally:
        conn.close()
