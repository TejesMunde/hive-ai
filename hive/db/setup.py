"""SQLite initialisation: tables, indexes, connection factory."""

import os
import sqlite3

DB_PATH = os.environ.get("HIVE_DB_PATH", "hive.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create tables + indexes if missing. Idempotent."""
    conn = get_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                id          TEXT PRIMARY KEY,
                project     TEXT NOT NULL,
                what        TEXT NOT NULL,
                why         TEXT,
                agent       TEXT,
                created_at  TEXT NOT NULL,
                confidence  REAL DEFAULT 1.0,
                -- Phase 3: a decision may replace an earlier decision
                -- (e.g. "switched from REST to gRPC"). Nullable self-reference.
                supersedes_id TEXT REFERENCES decisions(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id              TEXT PRIMARY KEY,
                project         TEXT NOT NULL,
                file_structure  TEXT NOT NULL,
                active_stack    TEXT,
                current_module  TEXT,
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS open_tasks (
                id              TEXT PRIMARY KEY,
                project         TEXT NOT NULL,
                description     TEXT NOT NULL,
                assigned_agent  TEXT,
                status          TEXT NOT NULL DEFAULT 'open',
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS staging (
                id          TEXT PRIMARY KEY,
                type        TEXT NOT NULL,
                project     TEXT NOT NULL,
                data        TEXT NOT NULL,
                reason      TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );

            -- Day 5: outcomes of past staging reviews. Used by `staging tune`
            -- to learn which guard categories are reliably wrong and should
            -- be auto-rejected up front instead of going to staging at all.
            CREATE TABLE IF NOT EXISTS staging_history (
                id          TEXT PRIMARY KEY,
                project     TEXT NOT NULL,
                type        TEXT NOT NULL,
                category    TEXT NOT NULL,
                outcome     TEXT NOT NULL,   -- 'accepted' | 'rejected'
                created_at  TEXT NOT NULL
            );

            -- Day 5: per-project, per-category policy. action='stage'
            -- (default) means the guard sends to staging for review.
            -- action='auto_reject' means the writer drops the record outright
            -- (after tuning). Per-project so what one team's reviewer rejects
            -- never bleeds into another project's behaviour.
            CREATE TABLE IF NOT EXISTS guard_policy (
                project      TEXT NOT NULL,
                category     TEXT NOT NULL,
                action       TEXT NOT NULL,     -- 'stage' | 'auto_reject'
                sample_size  INTEGER NOT NULL,
                accept_rate  REAL NOT NULL,
                updated_at   TEXT NOT NULL,
                PRIMARY KEY (project, category)
            );

            -- Day 6: append-only audit log of every meaningful action so we
            -- can soak Hive for a week and replay what happened.
            -- kind ∈ {'write_commit','write_staged','write_auto_rejected',
            --         'write_rejected','staging_accept','staging_reject',
            --         'task_close','query'}
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project     TEXT NOT NULL,
                kind        TEXT NOT NULL,
                payload     TEXT NOT NULL,   -- JSON
                created_at  TEXT NOT NULL
            );

            -- Day 7: cached float32 embeddings for hybrid retrieval.
            -- One row per decision. model + dim let us re-embed cleanly on
            -- model swap; vector stored as little-endian bytes for speed.
            CREATE TABLE IF NOT EXISTS decision_embeddings (
                decision_id  TEXT PRIMARY KEY,
                model        TEXT NOT NULL,
                dim          INTEGER NOT NULL,
                vector       BLOB NOT NULL,
                created_at   TEXT NOT NULL,
                FOREIGN KEY (decision_id) REFERENCES decisions(id) ON DELETE CASCADE
            );

            -- Phase 3: dead ends — approaches that were tried and rejected,
            -- linked to the decision that replaced each one. Nullable link so a
            -- dead end can be recorded before its replacement exists. SET NULL
            -- (not CASCADE) so deleting a decision never erases the record that
            -- alternatives were explored — the dead end outlives the decision.
            CREATE TABLE IF NOT EXISTS dead_ends (
                id                  TEXT PRIMARY KEY,
                project             TEXT NOT NULL,
                what_tried          TEXT NOT NULL,
                why_failed          TEXT NOT NULL,
                chosen_decision_id  TEXT,
                agent               TEXT,
                created_at          TEXT NOT NULL,
                confidence          REAL DEFAULT 1.0,
                FOREIGN KEY (chosen_decision_id) REFERENCES decisions(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_decisions_project   ON decisions(project);
            CREATE INDEX IF NOT EXISTS idx_snapshots_project   ON snapshots(project);
            CREATE INDEX IF NOT EXISTS idx_open_tasks_project  ON open_tasks(project);
            CREATE INDEX IF NOT EXISTS idx_open_tasks_status   ON open_tasks(status);
            CREATE INDEX IF NOT EXISTS idx_staging_project     ON staging(project);
            CREATE INDEX IF NOT EXISTS idx_history_category    ON staging_history(category);
            CREATE INDEX IF NOT EXISTS idx_audit_project_kind  ON audit_log(project, kind);
            CREATE INDEX IF NOT EXISTS idx_audit_created       ON audit_log(created_at);
            CREATE INDEX IF NOT EXISTS idx_dead_ends_project   ON dead_ends(project);
            CREATE INDEX IF NOT EXISTS idx_dead_ends_chosen    ON dead_ends(chosen_decision_id);
            """
        )
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent column adds for DBs created before a schema bump."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)")}
    if "supersedes_id" not in cols:
        # SQLite cannot ALTER-ADD a column with an inline REFERENCES clause; the
        # app-level FK usage + index is sufficient (FK enforced per insert).
        conn.execute("ALTER TABLE decisions ADD COLUMN supersedes_id TEXT")
