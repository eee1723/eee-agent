from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

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

# Ordered migrations. Each entry is (version, SQL script). The orchestrator
# splits the script into statements and runs them in one atomic transaction.
MIGRATIONS: tuple[tuple[int, str], ...] = ((1, MIGRATION_V1_SQL),)


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
