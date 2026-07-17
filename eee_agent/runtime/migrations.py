from __future__ import annotations

import hashlib
import sqlite3

SCHEMA_VERSION = 4

# Exact schema v1 DDL from the approved design spec section 7.3. No IF NOT
# EXISTS: a partially-wrong schema must surface, not be silently masked.
MIGRATION_V1_SQL = """
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    replay_floor_seq INTEGER NOT NULL DEFAULT 0
        CHECK (replay_floor_seq >= 0 AND replay_floor_seq <= last_seq)
);

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN (
        'Created', 'PreparingContext', 'Planning', 'Finalizing', 'Completed',
        'StopRequested', 'Stopping', 'Cancelled', 'Retrying', 'Failed'
    )),
    user_input TEXT NOT NULL,
    final_response TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    failure_json TEXT,
    model_snapshot_json TEXT NOT NULL
);

CREATE INDEX runs_by_session_created
    ON runs(session_id, created_at, run_id);

CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    run_id TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK (seq > 0),
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    retention_class TEXT NOT NULL
        CHECK (retention_class IN ('durable', 'operational')),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    UNIQUE(session_id, seq)
);

CREATE INDEX events_for_replay ON events(session_id, seq);

CREATE TABLE runtime_state (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    active_run_id TEXT REFERENCES runs(run_id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);
"""

# Exact schema v2 DDL (Task 16). Additive only: it creates the four typed
# changeset tables and leaves every v1 table untouched. No IF NOT EXISTS: a
# partially-wrong schema must surface, not be silently masked.
MIGRATION_V2_SQL = """
CREATE TABLE workspaces (
    workspace_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    instance_id TEXT NOT NULL,
    scene_epoch INTEGER NOT NULL CHECK (scene_epoch >= 1),
    revision TEXT NOT NULL CHECK (length(revision) = 64),
    created_by_run TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    updated_at TEXT NOT NULL,
    digest TEXT NOT NULL CHECK (length(digest) = 64),
    payload_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
);

CREATE INDEX workspaces_by_session ON workspaces(session_id, workspace_id);

CREATE TABLE changesets (
    change_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    workspace_id TEXT,
    digest TEXT NOT NULL CHECK (length(digest) = 64),
    state TEXT NOT NULL CHECK (state IN (
        'Proposed', 'AwaitingApproval', 'Approved', 'Applying', 'Applied',
        'RolledBack', 'CriticalRecovery', 'Stale', 'Rejected', 'Expired'
    )),
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
);

CREATE INDEX changesets_by_session ON changesets(session_id, change_id);
CREATE INDEX changesets_by_state ON changesets(state, change_id);

CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY,
    change_id TEXT NOT NULL REFERENCES changesets(change_id) ON DELETE CASCADE,
    changeset_digest TEXT NOT NULL CHECK (length(changeset_digest) = 64),
    decision TEXT NOT NULL CHECK (decision IN (
        'Pending', 'Approved', 'Rejected', 'Consumed', 'Expired'
    )),
    decided_by TEXT CHECK (decided_by IS NULL OR decided_by = 'local_user'),
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    expires_at TEXT NOT NULL,
    approved_instance_id TEXT,
    approved_scene_epoch INTEGER
        CHECK (approved_scene_epoch IS NULL OR approved_scene_epoch >= 1),
    updated_at TEXT NOT NULL,
    digest TEXT NOT NULL CHECK (length(digest) = 64),
    payload_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    UNIQUE(change_id)
);

CREATE INDEX approvals_by_change ON approvals(change_id);
CREATE INDEX approvals_by_decision ON approvals(decision, approval_id);

CREATE TABLE change_receipts (
    change_id TEXT PRIMARY KEY REFERENCES changesets(change_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN (
        'Applied', 'AlreadyApplied', 'RolledBack', 'Partial', 'CriticalRecovery'
    )),
    instance_id TEXT NOT NULL,
    scene_epoch INTEGER NOT NULL CHECK (scene_epoch >= 1),
    digest TEXT NOT NULL CHECK (length(digest) = 64),
    payload_json TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1)
);

CREATE INDEX change_receipts_by_status ON change_receipts(status, change_id);
"""

# Exact schema v3 DDL (Task 16-B2b). Additive only: it adds the composite
# parent key required by SQLite and the per-Session active Workspace pointer.
# Accepted v1/v2 script text remains byte-for-byte unchanged.
MIGRATION_V3_SQL = """
CREATE UNIQUE INDEX workspaces_identity_by_session
    ON workspaces(workspace_id, session_id);

CREATE TABLE session_workspace_state (
    session_id TEXT PRIMARY KEY
        REFERENCES sessions(session_id) ON DELETE CASCADE,
    active_workspace_id TEXT NOT NULL,
    state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
    updated_at TEXT NOT NULL,
    FOREIGN KEY(active_workspace_id, session_id)
        REFERENCES workspaces(workspace_id, session_id)
);
"""

# Exact schema v4 DDL (Task 19-A). Additive only: it creates the typed
# content-addressed artifact metadata table and leaves every v1-v3 table
# untouched. Accepted v1-v3 script text remains byte-for-byte unchanged.
MIGRATION_V4_SQL = """
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    redacted INTEGER NOT NULL DEFAULT 0 CHECK (redacted IN (0, 1)),
    created_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    UNIQUE(session_id, relative_path)
);

CREATE INDEX artifacts_by_session ON artifacts(session_id, created_at, artifact_id);
CREATE INDEX artifacts_by_sha256 ON artifacts(sha256, artifact_id);
"""

# Ordered migrations. Each entry is (version, SQL script). The orchestrator
# splits the script into statements and runs them in one atomic transaction.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, MIGRATION_V1_SQL),
    (2, MIGRATION_V2_SQL),
    (3, MIGRATION_V3_SQL),
    (4, MIGRATION_V4_SQL),
)


def migration_checksum(script: str) -> str:
    """Deterministic SHA-256 of a migration's exact SQL script text.

    The checksum covers the precise script stored in :data:`MIGRATIONS`, so any
    tampering with an applied migration's recorded checksum (or a script change
    without a version bump) is detectable on the next open.
    """
    if type(script) is not str:
        raise TypeError("migration script must be a string")
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def script_for_version(version: int) -> str | None:
    """Return the exact SQL script registered for ``version``, or ``None``."""
    for stored_version, script in MIGRATIONS:
        if stored_version == version:
            return script
    return None


def split_sql_statements(script: str) -> list[str]:
    """Split a SQL script into individual statements.

    Uses :func:`sqlite3.complete_statement` so statement boundaries honour
    string literals and comments the same way the executing engine does. This
    deliberately avoids the implicit-committing ``executescript`` and the
    fragile ``str.split(';')``.
    """
    statements: list[str] = []
    remaining = script.strip()
    while remaining:
        # Find the shortest prefix ending in ';' that parses as a complete
        # statement. A ';' inside a string literal leaves the prefix incomplete,
        # so it is skipped.
        cut = 0
        statement_end: int | None = None
        while True:
            semi = remaining.find(";", cut)
            if semi == -1:
                break
            if sqlite3.complete_statement(remaining[: semi + 1]):
                statement_end = semi
                break
            cut = semi + 1
        if statement_end is None:
            tail = remaining.strip()
            if tail:
                statements.append(tail)
            break
        stmt = remaining[: statement_end + 1].strip()
        if stmt:
            statements.append(stmt)
        remaining = remaining[statement_end + 1:].lstrip()
    return statements
