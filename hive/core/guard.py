import json
import sqlite3
from difflib import SequenceMatcher
from hive.db.setup import get_connection

MIN_WHAT_WORDS   = 5
FUZZY_THRESHOLD  = 0.45   # Jaccard token overlap above which we flag as duplicate


def validate(record_type: str, project: str, data: dict) -> tuple[bool, str]:
    """
    Returns (is_valid, reason).
    Invalid records go to staging, not the bin.
    """

    # Rule 1: required fields must be present and non-empty
    required = {
        "decision":  ["what"],
        "snapshot":  ["file_structure"],
        "open_task": ["description"],
        "dead_end":  ["what_tried", "why_failed"],
    }
    fields = required.get(record_type, [])
    for field in fields:
        value = data.get(field, "").strip()
        if not value:
            return False, f"Missing or empty required field: '{field}'"

    # Rule 2: reject vague entries
    main_field = {
        "decision":  "what",
        "snapshot":  "file_structure",
        "open_task": "description",
        "dead_end":  "what_tried",
    }.get(record_type, "")

    if main_field:
        value = data.get(main_field, "")
        if record_type == "snapshot":
            segment_count = max(len(value.split(",")), len(value.split("/")))
            if segment_count < 2 and len(value.strip()) < 10:
                return False, f"Too vague — '{main_field}' needs more detail"
        else:
            word_count = len(value.split())
            if word_count < MIN_WHAT_WORDS:
                return False, (
                    f"Too vague — '{main_field}' needs at least "
                    f"{MIN_WHAT_WORDS} words (got {word_count})"
                )

    # Rule 3: exact duplicate check
    if record_type == "decision":
        dup = _find_exact_duplicate_decision(project, data["what"])
        if dup:
            return False, f"Exact duplicate: '{data['what'][:60]}'"

    if record_type == "open_task":
        dup = _find_exact_duplicate_task(project, data["description"])
        if dup:
            return False, f"Exact duplicate task: '{data['description'][:60]}'"

    # Rule 4: contradiction check — decisions only.
    # Runs BEFORE fuzzy dup so opposing rewordings (same nouns, flipped
    # marker) are not misclassified as duplicates.
    if record_type == "decision":
        conflict = _find_contradiction(project, data["what"])
        if conflict:
            return False, f"Contradicts existing decision: '{conflict[:80]}'"

    # Rule 5: fuzzy duplicate check (catches near-identical rewordings)
    if record_type == "decision":
        fuzzy = _find_fuzzy_duplicate_decision(project, data["what"])
        if fuzzy:
            return False, (
                f"Too similar to existing decision "
                f"({int(fuzzy['score']*100)}% match): '{fuzzy['what'][:70]}'"
            )

    if record_type == "open_task":
        fuzzy = _find_fuzzy_duplicate_task(project, data["description"])
        if fuzzy:
            return False, (
                f"Too similar to existing task "
                f"({int(fuzzy['score']*100)}% match): '{fuzzy['description'][:70]}'"
            )

    if record_type == "dead_end":
        fuzzy = _find_fuzzy_duplicate_dead_end(project, data["what_tried"])
        if fuzzy:
            return False, (
                f"Too similar to existing dead end "
                f"({int(fuzzy['score']*100)}% match): '{fuzzy['what_tried'][:70]}'"
            )

    # Rule 6: why field warning — not a rejection, just flagged in reason
    if record_type == "decision" and not data.get("why", "").strip():
        return False, "Missing 'why' field — decisions without reasoning lose value over time"

    return True, "ok"


def send_to_staging(record_type: str, project: str, data: dict, reason: str):
    import uuid
    from datetime import datetime, timezone

    conn = get_connection()
    conn.execute(
        "INSERT INTO staging (id, type, project, data, reason, created_at) VALUES (?,?,?,?,?,?)",
        (
            str(uuid.uuid4()),
            record_type,
            project,
            json.dumps(data),
            reason,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    print(f"[hive] Staged (not committed) — reason: {reason}")


# ── Internal helpers ──────────────────────────────────────────────────────────

_STOP = {"a","an","the","is","in","on","at","to","for","of","and","or","but","we","our","this","that"}

def _similarity(a: str, b: str) -> float:
    """Jaccard token overlap — better than SequenceMatcher for semantic duplicates."""
    def tokens(s):
        return set(w.strip(".,!?") for w in s.lower().split() if w not in _STOP and len(w) > 2)
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _find_exact_duplicate_decision(project: str, what: str) -> bool:
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM decisions WHERE project=? AND LOWER(what)=LOWER(?)",
        (project, what.strip()),
    ).fetchone()
    conn.close()
    return row is not None


def _find_exact_duplicate_task(project: str, description: str) -> bool:
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM open_tasks WHERE project=? AND LOWER(description)=LOWER(?) AND status='open'",
        (project, description.strip()),
    ).fetchone()
    conn.close()
    return row is not None


def _find_fuzzy_duplicate_decision(project: str, what: str) -> dict | None:
    conn = get_connection()
    rows = conn.execute(
        "SELECT what FROM decisions WHERE project=?", (project,)
    ).fetchall()
    conn.close()

    for row in rows:
        score = _similarity(what, row["what"])
        if score >= FUZZY_THRESHOLD:
            return {"what": row["what"], "score": score}
    return None


def _find_fuzzy_duplicate_task(project: str, description: str) -> dict | None:
    conn = get_connection()
    rows = conn.execute(
        "SELECT description FROM open_tasks WHERE project=? AND status='open'", (project,)
    ).fetchall()
    conn.close()

    for row in rows:
        score = _similarity(description, row["description"])
        if score >= FUZZY_THRESHOLD:
            return {"description": row["description"], "score": score}
    return None


def _find_fuzzy_duplicate_dead_end(project: str, what_tried: str) -> dict | None:
    conn = get_connection()
    rows = conn.execute(
        "SELECT what_tried FROM dead_ends WHERE project=?", (project,)
    ).fetchall()
    conn.close()

    for row in rows:
        score = _similarity(what_tried, row["what_tried"])
        if score >= FUZZY_THRESHOLD:
            return {"what_tried": row["what_tried"], "score": score}
    return None


def _find_contradiction(project: str, new_what: str) -> str | None:
    """
    Contradiction = same nouns around an opposition marker, swapped sides.

    Example:
        existing: "PostgreSQL chosen as the primary database over SQLite"
        new:      "Using SQLite over PostgreSQL for the primary database"
        marker = " over " — new_left tokens ⊆ existing_right tokens
                            new_right tokens ⊆ existing_left tokens
    """
    markers = [" vs ", " over ", " instead of ", " not ", " rather than "]
    new_lower = new_what.lower()

    def side_tokens(s: str) -> set[str]:
        return {
            w.strip(".,!?")
            for w in s.split()
            if w not in _STOP and len(w.strip(".,!?")) > 2
        }

    conn = get_connection()
    rows = conn.execute(
        "SELECT what FROM decisions WHERE project=?", (project,)
    ).fetchall()
    conn.close()

    for row in rows:
        existing = row["what"].lower()
        for marker in markers:
            if marker not in new_lower or marker not in existing:
                continue
            n_left, n_right = new_lower.split(marker, 1)
            e_left, e_right = existing.split(marker, 1)
            nL, nR = side_tokens(n_left),  side_tokens(n_right)
            eL, eR = side_tokens(e_left),  side_tokens(e_right)

            # Swapped overlap on both sides, and not just same-side overlap.
            swapped   = bool(nL & eR) and bool(nR & eL)
            same_side = bool(nL & eL) and bool(nR & eR)
            if swapped and not same_side:
                return row["what"]
    return None
