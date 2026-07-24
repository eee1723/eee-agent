# Node Lifecycle & Task Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the sandbox→verify→commit workflow a memory and a finishing touch: record every build step and its nodes in a per-run task graph (SQLite), inject a compact summary into every LLM call, auto-finalize commits (deterministic layered layout, display/render flags, node comments), and let the agent clean up orphan nodes through a two-phase `cleanup_nodes` tool — plus a minimal read-only panel view.

**Architecture:** Runtime SQLite gains `task_steps`/`task_nodes` (migration v8) behind a new `TaskGraphStore` repository (`eee_agent/runtime/task_graph.py`). The `scratch.py` DTO layer gains `purpose` (exec), `note` (create_node op), `annotations` (commit), a `delete_node` op kind, and two new operations (`scratch.topology` read-only, `scratch.delete` allowlisted write) gated on a new additive `scratch.v2` capability. The Houdini-side executor finalizes commits best-effort inside the existing promote undo group and deletes only runtime-allowlisted paths. `ScratchCoordinator` records steps/nodes (degrading gracefully when the store fails) and assembles commit annotations from the graph. A LangChain middleware appends the ≤2KB summary to the system message per model call. The panel reads steps over the existing runtime WebSocket protocol (`task_graph.list`).

**Tech Stack:** Python 3.11 (uv-managed), aiosqlite + `RuntimeDatabase` migrations, frozen/slotted dataclass DTOs with canonical JSON, LangChain `AgentMiddleware`, PySide6 panel (Qt-free core in `eee_agent/panel/`), pytest offline gate (`uv run --frozen --extra eval pytest -q`), hython smoke scripts.

**Source of truth:** `docs/superpowers/specs/2026-07-24-node-lifecycle-task-graph-design.md` (approved design). Where this plan and the spec disagree, the code reality documented in "Assumptions & deviations" below wins and the spec gets an implementation note in Task 10.

**Conventions in force (from CLAUDE.md + code):**
- Test command: `uv run --frozen --extra eval pytest -q <path>` (single file) / `uv run --frozen --extra eval pytest -q` (full gate, baseline 3458 passed, 11 skipped).
- Runtime tests avoid pytest-asyncio: drive coroutines with `asyncio.run` via a `_run` helper.
- DTO strictness: frozen slotted dataclasses, exact field sets, canonical JSON, bounded strings, `type(x) is ...` validation.
- The executor never imports `hou` at module level; all HOM access via the injected scene adapter; offline tests inject a hou-free fake scene.
- All mutations inside one `hou.undos.group("EEE Agent - ...")`; terminal cleanup under `hou.undos.disabler()`.
- New bridge surfaces are additive capabilities, never protocol changes; server fails closed before parsing when a capability is missing.
- Commit style (from `git log`): conventional commits, e.g. `feat(runtime): ...`, `fix(scratch): ...`, `docs: ...`.

---

## Assumptions & deviations from the spec

These were forced by code reality found while planning; each is called out in the task that implements it.

1. **Topology checks cannot use the existing read-only `query_scene`.** `SelectedNode` (`eee_agent/houdini_bridge/contracts.py:237-281`) carries only path/type/parent/name/lock/geometry — no wiring. The spec's "拓扑检查通过现有只读桥接查询" is therefore implemented as a new additive read-only operation `scratch.topology` under the new `scratch.v2` capability (per-path inputs/outputs/display-flag), instead of modifying the frozen scene.query contract.
2. **New capability is `scratch.v2`** (spec allowed "`scratch.v2` 或独立 `cleanup.v1`"). It covers: the `delete_node` op kind in `scratch.exec`, `scratch.delete`, and `scratch.topology`.
3. **`purpose` is a required field on the `scratch.exec` wire DTO.** Because a required DTO field cannot land half-wired without breaking the build, Task 2 threads `purpose` through provider → coordinator → `scratch_build` tool in one commit; Task 6 then adds store recording on top (the user's task split mentioned the purpose param under Task 6 — it necessarily lands in Task 2).
4. **Commit `annotations` travel as a sorted tuple of `(node_name, comment)` pairs** inside the frozen dataclass (JSON object on the wire), mirroring the `orientation_checks` validation style. Executor converts to a dict for lookup.
5. **`ScratchCommitResult` gains a `warnings` field** (bounded like `errors`) so best-effort finalization failures reach the agent. Runtime and Houdini side ship from the same repo, so the exact-keys result contract stays consistent.
6. **Layout anchor = (min x, max y) of the committed nodes' current positions.** The spec says "整体平移到节点默认位置的左上角锚点"; Houdini's default placement cascades, so the top-left of the current bounding box is the deterministic anchor.
7. **Suggest phase of `cleanup_nodes` uses "no downstream consumers" as the safety criterion** (covers "not in the output chain" and "unwired" for leaf nodes). Chained orphans are deleted leaf-first across repeated calls. This is the minimal deterministic rule; no fancy graph analysis.
8. **Panel Qt wiring is manual-verify.** The Qt-free parser (`eee_agent/panel/runtime_state.py`) and the runtime server command are unit-tested offline; the PySide6 widget itself cannot be instantiated offline (no Qt in the test venv) and follows the existing lazy-import pattern. Its verification is part of the Task 10 hython/manual pass.
9. **The inspector read-only whitelist (`tests/runtime/test_workspace_bridge_inspector.py:347-348`) is untouched.** That constraint scopes the read-only inspector tool; this feature sets display/render flags on the commit write path, which is a different surface. CLAUDE.md gets the clarification note in Task 10.
10. **`RuntimeService` exposes `self._database`** (it constructs repositories from the `database` argument in `__init__`); `TaskGraphStore` is built from it there.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `eee_agent/runtime/migrations.py` | Modify | `MIGRATION_V8_SQL` (`task_steps`, `task_nodes`), `SCHEMA_VERSION = 8`, `MIGRATIONS` entry |
| `eee_agent/runtime/task_graph.py` | Create | `TaskStep`/`TaskNode` dataclasses, `TaskGraphStore` (record/list/mark/summary), `TaskGraphToolContext` |
| `tests/runtime/test_task_graph.py` | Create | Store + migration v8 tests |
| `tests/runtime/test_database.py` | Modify | Bump hard-coded schema v7 → v8 assertions |
| `eee_agent/houdini_bridge/scratch.py` | Modify | `purpose`/`note`/`annotations`/`warnings` DTO fields, `delete_node` op kind, `SCRATCH_V2`, `scratch.delete` + `scratch.topology` DTOs |
| `tests/runtime/test_scratch_bridge.py` | Modify | DTO strictness tests, client/provider tests, coordinator + cleanup tests |
| `eee_agent/houdini_bridge/client.py` | Modify | `scratch_delete_nodes`, `scratch_topology`, `scratch.v2` gating, delete-op gate in `scratch_exec` |
| `eee_agent/houdini_bridge/changeset_provider.py` | Modify | Thread `purpose`/`annotations`; `delete_nodes`, `scene_topology` |
| `houdini_side/secure_bridge.py` | Modify | Advertise `scratch.v2`; dispatch arms + handlers for `scratch.delete`/`scratch.topology`; delete-op admission check |
| `houdini_side/changeset_executor.py` | Modify | `_layered_layout` pure fn, `_finalize_commit` (layout/flags/comments), `delete_nodes`, `scratch_topology`, `delete_node` op |
| `tests/runtime/test_scratch_finalize.py` | Create | Offline fake-scene executor tests for layout/flags/comments/delete/topology |
| `eee_agent/modeling/scratch_coordinator.py` | Modify | `purpose` param, store recording on build/commit, annotation assembly, `cleanup()` + `cleanup_nodes`/`task_graph_status` tools |
| `eee_agent/runtime/agent_context.py` | Modify | `RuntimeToolContext.task_graph` field |
| `eee_agent/runtime/service.py` | Modify | Construct `TaskGraphStore`; wire into scratch + runtime contexts; `list_task_steps` |
| `eee_agent/runtime/agent_runner.py` | Modify | Register `cleanup_nodes` + `task_graph_status` in modeling mode |
| `eee_agent/task_summary.py` | Create | `TaskSummaryMiddleware` (per-call summary injection) |
| `eee_agent/app.py` | Modify | Register the middleware |
| `tests/runtime/test_task_summary.py` | Create | Middleware injection tests |
| `eee_agent/runtime/protocol.py` | Modify | `task_graph.list` command type |
| `eee_agent/runtime/server.py` | Modify | `task_graph.list` command arm |
| `eee_agent/panel/runtime_state.py` | Modify | `parse_task_graph_list` + `format_task_graph_steps` (Qt-free) |
| `houdini_side/runtime_panel/task_graph_panel.py` | Create | Read-only text widget (manual verify) |
| `houdini_side/runtime_panel/client.py` | Modify | `refresh_task_graph` + response handling + signal |
| `houdini_side/runtime_panel/conversation.py` | Modify | Host the new panel block |
| `tests/runtime/test_task_graph_panel.py` | Create | Qt-free parser + server command tests |
| `tests/runtime/task_graph_houdini_smoke.py` | Create | Real-Houdini smoke (manual, hython) |
| `CLAUDE.md` | Modify | New tools, `scratch.v2`, schema v8, inspector-whitelist clarification |
| `docs/superpowers/specs/2026-07-24-node-lifecycle-task-graph-design.md` | Modify | Implementation notes (deviations 1-8) |

---

## Task 1 — Migration v8 + `eee_agent/runtime/task_graph.py` store

**Files:**
- Modify: `eee_agent/runtime/migrations.py` (line 6 `SCHEMA_VERSION`, after line 233 `MIGRATION_V7_SQL` block, lines 237-245 `MIGRATIONS` tuple)
- Create: `eee_agent/runtime/task_graph.py`
- Test: `tests/runtime/test_task_graph.py` (create)
- Modify: `tests/runtime/test_database.py` (schema-version assertions, e.g. lines 97-128, 135-149, and the checksum/version-list tests near lines 861-1070)

- [ ] **Step 1.1 — Write the failing store tests.** Create `tests/runtime/test_task_graph.py`:

```python
"""Task graph store: migration v8 tables, recording, lifecycle, summary.

Async scenarios run via ``asyncio.run`` (no pytest-asyncio), matching the rest
of the Runtime tests. Each test drives the store against a fresh
``RuntimeDatabase`` opened on a tmp path.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.task_graph import (
    TaskGraphStore,
    TaskGraphToolContext,
)


def _run(coro):
    return asyncio.run(coro)


async def _open(db_path: Path) -> tuple[RuntimeDatabase, TaskGraphStore]:
    db = await RuntimeDatabase.open(db_path)
    return db, TaskGraphStore(db)


async def _seed_run(db: RuntimeDatabase, run_id: str = "run_1") -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with db.write_transaction() as conn:
        await conn.execute(
            "INSERT INTO sessions(session_id, title, status, created_at, "
            "updated_at, last_seq, replay_floor_seq) "
            "VALUES ('sess_1', 't', 'active', ?, ?, 0, 0)",
            (now, now),
        )
        await conn.execute(
            "INSERT INTO runs(run_id, session_id, status, user_input, "
            "created_at, model_snapshot_json) "
            "VALUES (?, 'sess_1', 'Planning', 'build a table', ?, '{}')",
            (run_id, now),
        )


def test_schema_v8_creates_task_tables(db_path: Path) -> None:
    async def scenario() -> None:
        db, _store = await _open(db_path)
        try:
            assert await db.schema_version() == 8
            names = await db.table_names()
            assert {"task_steps", "task_nodes"} <= names
        finally:
            await db.close()

    _run(scenario())


def test_record_step_assigns_monotonic_seq(db_path: Path) -> None:
    async def scenario() -> None:
        db, store = await _open(db_path)
        try:
            await _seed_run(db)
            first = await store.record_step(
                run_id="run_1", tool="scratch_build", purpose="创建四条桌腿"
            )
            second = await store.record_step(
                run_id="run_1", tool="scratch_build", purpose="桌面建模"
            )
            assert first.seq == 1
            assert second.seq == 2
            assert first.status == "open"
            assert first.step_id != second.step_id
            assert first.created_at.utcoffset() is not None
        finally:
            await db.close()

    _run(scenario())


def test_record_step_validation(db_path: Path) -> None:
    async def scenario() -> None:
        db, store = await _open(db_path)
        try:
            await _seed_run(db)
            with pytest.raises(ValueError):
                await store.record_step(run_id="run_1", tool="move_node", purpose="x")
            with pytest.raises(ValueError):
                await store.record_step(run_id="run_1", tool="scratch_build", purpose="  ")
            with pytest.raises(ValueError):
                await store.record_step(
                    run_id="run_1", tool="scratch_build", purpose="x" * 201
                )
            with pytest.raises(ValueError):
                await store.record_step(run_id="", tool="scratch_build", purpose="x")
        finally:
            await db.close()

    _run(scenario())


def test_record_nodes_and_list_run_steps(db_path: Path) -> None:
    async def scenario() -> None:
        db, store = await _open(db_path)
        try:
            await _seed_run(db)
            step = await store.record_step(
                run_id="run_1", tool="scratch_build", purpose="桌腿"
            )
            nodes = await store.record_nodes(
                step_id=step.step_id,
                nodes=[
                    ("/obj/eee_scratch_run_1/leg1", "tube", "第一条腿"),
                    ("/obj/eee_scratch_run_1/xform1", "xform", None),
                ],
            )
            assert len(nodes) == 2
            assert nodes[0].status == "sandbox"
            assert nodes[0].note == "第一条腿"
            assert nodes[1].note is None
            steps = await store.list_run_steps("run_1")
            assert len(steps) == 1
            assert [n.node_path for n in steps[0].nodes] == [
                "/obj/eee_scratch_run_1/leg1",
                "/obj/eee_scratch_run_1/xform1",
            ]
        finally:
            await db.close()

    _run(scenario())


def test_mark_committed_rewrites_paths_and_status(db_path: Path) -> None:
    async def scenario() -> None:
        db, store = await _open(db_path)
        try:
            await _seed_run(db)
            step = await store.record_step(
                run_id="run_1", tool="scratch_build", purpose="桌面"
            )
            await store.record_nodes(
                step_id=step.step_id,
                nodes=[("/obj/eee_scratch_run_1/box1", "box", None)],
            )
            await store.mark_committed(
                run_id="run_1",
                sandbox_root="/obj/eee_scratch_run_1",
                final_path="/obj/table1",
            )
            steps = await store.list_run_steps("run_1")
            assert steps[0].status == "committed"
            node = steps[0].nodes[0]
            assert node.status == "committed"
            assert node.committed_path == "/obj/table1/box1"
        finally:
            await db.close()

    _run(scenario())


def test_mark_deleted_marks_nodes_and_closes_steps(db_path: Path) -> None:
    async def scenario() -> None:
        db, store = await _open(db_path)
        try:
            await _seed_run(db)
            step = await store.record_step(
                run_id="run_1", tool="scratch_build", purpose="草稿"
            )
            await store.record_nodes(
                step_id=step.step_id,
                nodes=[
                    ("/obj/eee_scratch_run_1/a", "box", None),
                    ("/obj/eee_scratch_run_1/b", "grid", None),
                ],
            )
            count = await store.mark_deleted(
                run_id="run_1", node_paths=["/obj/eee_scratch_run_1/a"]
            )
            assert count == 1
            steps = await store.list_run_steps("run_1")
            assert steps[0].status == "open"  # node b still live
            count = await store.mark_deleted(
                run_id="run_1", node_paths=["/obj/eee_scratch_run_1/b"]
            )
            assert count == 1
            steps = await store.list_run_steps("run_1")
            assert steps[0].status == "deleted"
            # Idempotent: deleting again changes nothing.
            assert await store.mark_deleted(
                run_id="run_1", node_paths=["/obj/eee_scratch_run_1/a"]
            ) == 0
        finally:
            await db.close()

    _run(scenario())


def test_render_summary_none_without_steps(db_path: Path) -> None:
    async def scenario() -> None:
        db, store = await _open(db_path)
        try:
            await _seed_run(db)
            assert await store.render_summary("run_1") is None
        finally:
            await db.close()

    _run(scenario())


def test_render_summary_shape_and_current_output(db_path: Path) -> None:
    async def scenario() -> None:
        db, store = await _open(db_path)
        try:
            await _seed_run(db)
            step = await store.record_step(
                run_id="run_1", tool="scratch_build", purpose="桌面建模"
            )
            await store.record_nodes(
                step_id=step.step_id,
                nodes=[("/obj/eee_scratch_run_1/blast1", "blast", "输出")],
            )
            await store.mark_committed(
                run_id="run_1",
                sandbox_root="/obj/eee_scratch_run_1",
                final_path="/obj/table1",
            )
            summary = await store.render_summary("run_1")
            assert summary is not None
            assert "Task state (run run_1):" in summary
            assert "1. [committed] 桌面建模 (blast1)" in summary
            assert "Current output: /obj/table1/blast1" in summary
        finally:
            await db.close()

    _run(scenario())


def test_render_summary_truncates_to_ten_steps_and_2kb(db_path: Path) -> None:
    async def scenario() -> None:
        db, store = await _open(db_path)
        try:
            await _seed_run(db)
            for i in range(20):
                await store.record_step(
                    run_id="run_1",
                    tool="scratch_build",
                    purpose=f"步骤{i} " + "长" * 100,
                )
            summary = await store.render_summary("run_1")
            assert summary is not None
            assert len(summary.encode("utf-8")) <= 2048
            assert "earlier step(s) omitted" in summary
            # The most recent steps survive truncation.
            assert "步骤19" in summary
        finally:
            await db.close()

    _run(scenario())


def test_tool_context_validation(db_path: Path) -> None:
    async def scenario() -> None:
        db, store = await _open(db_path)
        try:
            ctx = TaskGraphToolContext(store=store, run_id="run_1")
            assert ctx.run_id == "run_1"
            with pytest.raises(TypeError):
                TaskGraphToolContext(store=object(), run_id="run_1")  # type: ignore[arg-type]
            with pytest.raises(TypeError):
                TaskGraphToolContext(store=store, run_id="")
        finally:
            await db.close()

    _run(scenario())
```

- [ ] **Step 1.2 — Run the new tests; expect import failure.** `uv run --frozen --extra eval pytest -q tests/runtime/test_task_graph.py` → fails with `ModuleNotFoundError: No module named 'eee_agent.runtime.task_graph'`.

- [ ] **Step 1.3 — Add migration v8.** In `eee_agent/runtime/migrations.py`: change line 6 to `SCHEMA_VERSION = 8`; after the `MIGRATION_V7_SQL` block add:

```python
# Exact schema v8 DDL (node lifecycle & task graph). Additive only: it creates
# the per-run task graph tables and leaves every v1-v7 table untouched. No
# IF NOT EXISTS: a partially-wrong schema must surface, not be silently masked.
MIGRATION_V8_SQL = """
CREATE TABLE task_steps (
    step_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK (seq > 0),
    tool TEXT NOT NULL CHECK (tool IN (
        'scratch_build', 'scratch_commit', 'cleanup_nodes'
    )),
    purpose TEXT NOT NULL CHECK (length(purpose) <= 200),
    status TEXT NOT NULL CHECK (status IN ('open', 'committed', 'deleted')),
    created_at TEXT NOT NULL,
    UNIQUE(run_id, seq)
);

CREATE INDEX task_steps_by_run ON task_steps(run_id, seq);

CREATE TABLE task_nodes (
    node_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    step_id INTEGER NOT NULL REFERENCES task_steps(step_id) ON DELETE CASCADE,
    node_path TEXT NOT NULL,
    committed_path TEXT,
    node_type TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL CHECK (status IN ('sandbox', 'committed', 'deleted')),
    UNIQUE(run_id, node_path)
);

CREATE INDEX task_nodes_by_run ON task_nodes(run_id, status);
"""
```

Append `(8, MIGRATION_V8_SQL),` to the `MIGRATIONS` tuple (after the `(7, MIGRATION_V7_SQL),` entry).

- [ ] **Step 1.4 — Create the store module.** Create `eee_agent/runtime/task_graph.py`:

```python
"""Per-run task graph: which nodes each agent step created, and why.

One row in ``task_steps`` = one tool call (``scratch_build`` / ``cleanup_nodes``)
with the LLM-supplied purpose; one row in ``task_nodes`` = one node the agent
created, with its lifecycle status (``sandbox`` → ``committed`` → ``deleted``).
The graph deliberately stores NO wiring edges: live topology always comes from
the Houdini scene (via the bridge), so the graph can never disagree with the
scene. It only stores what the scene cannot answer: purpose, step ownership,
and lifecycle state.

The module follows the existing repository pattern (see
:mod:`eee_agent.runtime.sessions`): the store takes a
:class:`~eee_agent.runtime.database.RuntimeDatabase`, writes inside
``write_transaction``, reads via ``fetchone``/``fetchall``, and validates with
exact ``type(x) is ...`` checks. It never touches the events table and never
imports ``hou`` or bridge modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from eee_agent.runtime.database import RuntimeDatabase

_MAX_PURPOSE_CHARS = 200
_MAX_NOTE_CHARS = 200
# Summary injection budget: the most recent steps, capped so the injected
# system-message block stays tiny next to tool outputs (16KB tool cap).
_SUMMARY_MAX_STEPS = 10
_SUMMARY_MAX_BYTES = 2048

_STEP_TOOLS = frozenset({"scratch_build", "scratch_commit", "cleanup_nodes"})

_STEP_COLUMNS = "step_id, run_id, seq, tool, purpose, status, created_at"
_NODE_COLUMNS = (
    "node_id, step_id, node_path, committed_path, node_type, note, status"
)


@dataclass(frozen=True, slots=True)
class TaskNode:
    """One agent-created node and its lifecycle state."""

    node_id: int
    step_id: int
    node_path: str
    committed_path: str | None
    node_type: str
    note: str | None
    status: str  # "sandbox" | "committed" | "deleted"


@dataclass(frozen=True, slots=True)
class TaskStep:
    """One recorded tool-call step with the nodes it created."""

    step_id: int
    run_id: str
    seq: int
    tool: str
    purpose: str
    status: str  # "open" | "committed" | "deleted"
    created_at: datetime
    nodes: tuple[TaskNode, ...] = ()


def _require_run_id(run_id: object) -> str:
    if type(run_id) is not str or not run_id:
        raise ValueError("run_id must be a non-empty string")
    return run_id


def _require_purpose(purpose: object) -> str:
    if type(purpose) is not str or not purpose.strip():
        raise ValueError("purpose must be a non-empty string")
    if len(purpose) > _MAX_PURPOSE_CHARS:
        raise ValueError(
            f"purpose must be at most {_MAX_PURPOSE_CHARS} characters"
        )
    return purpose


class TaskGraphStore:
    """Durable per-run task graph persistence.

    Records steps and nodes, migrates their lifecycle status, and renders the
    compact summary injected into the LLM context and shown in the panel. All
    methods are safe to call concurrently; writes serialize on the database
    write lock like every other Runtime repository.
    """

    def __init__(self, database: RuntimeDatabase) -> None:
        if type(database) is not RuntimeDatabase:
            raise TypeError("database must be a RuntimeDatabase")
        self._database = database

    async def record_step(self, *, run_id: str, tool: str, purpose: str) -> TaskStep:
        """Append one step for ``run_id``; seq is per-run monotonic from 1."""
        rid = _require_run_id(run_id)
        if tool not in _STEP_TOOLS:
            raise ValueError(f"tool must be one of {sorted(_STEP_TOOLS)}")
        checked_purpose = _require_purpose(purpose)
        now = datetime.now(timezone.utc)
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
                "FROM task_steps WHERE run_id = ?",
                (rid,),
            )
            seq = (await cursor.fetchone())["next_seq"]
            cursor = await conn.execute(
                "INSERT INTO task_steps(run_id, seq, tool, purpose, status, created_at) "
                "VALUES (?, ?, ?, ?, 'open', ?)",
                (rid, seq, tool, checked_purpose, now.isoformat()),
            )
            step_id = cursor.lastrowid
        return TaskStep(
            step_id=step_id,
            run_id=rid,
            seq=seq,
            tool=tool,
            purpose=checked_purpose,
            status="open",
            created_at=now,
        )

    async def record_nodes(
        self, *, step_id: int, nodes: Sequence[tuple[str, str, str | None]]
    ) -> tuple[TaskNode, ...]:
        """Attach created nodes to a step; each is ``(path, node_type, note)``."""
        if type(step_id) is not int or isinstance(step_id, bool) or step_id < 1:
            raise ValueError("step_id must be a positive integer")
        prepared: list[tuple[str, str, str | None]] = []
        for item in nodes:
            path, node_type, note = item
            if type(path) is not str or not path.startswith("/"):
                raise ValueError("node_path must be an absolute path")
            if type(node_type) is not str or not node_type:
                raise ValueError("node_type must be a non-empty string")
            if note is not None and (
                type(note) is not str or len(note) > _MAX_NOTE_CHARS
            ):
                raise ValueError(
                    f"note must be a string of at most {_MAX_NOTE_CHARS} characters"
                )
            prepared.append((path, node_type, note))
        out: list[TaskNode] = []
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT run_id FROM task_steps WHERE step_id = ?", (step_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise ValueError("step_id does not exist")
            run_id = row["run_id"]
            for path, node_type, note in prepared:
                cursor = await conn.execute(
                    "INSERT INTO task_nodes(run_id, step_id, node_path, "
                    "committed_path, node_type, note, status) "
                    "VALUES (?, ?, ?, NULL, ?, ?, 'sandbox')",
                    (run_id, step_id, path, node_type, note),
                )
                out.append(
                    TaskNode(
                        node_id=cursor.lastrowid,
                        step_id=step_id,
                        node_path=path,
                        committed_path=None,
                        node_type=node_type,
                        note=note,
                        status="sandbox",
                    )
                )
        return tuple(out)

    async def mark_committed(
        self, *, run_id: str, sandbox_root: str, final_path: str
    ) -> None:
        """Mark the run's sandbox nodes committed under ``final_path``.

        Mirrors the executor's promotion semantics: the sandbox container is
        renamed, so a node at ``<sandbox_root>/<name>`` lands at
        ``<final_path>/<name>``. Open steps of the run become ``committed``.
        """
        rid = _require_run_id(run_id)
        if type(sandbox_root) is not str or not sandbox_root.startswith("/obj/"):
            raise ValueError("sandbox_root must be an /obj/ path")
        if type(final_path) is not str or not final_path.startswith("/"):
            raise ValueError("final_path must be an absolute path")
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT node_id, node_path FROM task_nodes "
                "WHERE run_id = ? AND status = 'sandbox'",
                (rid,),
            )
            for row in await cursor.fetchall():
                suffix = row["node_path"][len(sandbox_root):]
                await conn.execute(
                    "UPDATE task_nodes SET committed_path = ?, status = 'committed' "
                    "WHERE node_id = ?",
                    (final_path + suffix, row["node_id"]),
                )
            await conn.execute(
                "UPDATE task_steps SET status = 'committed' "
                "WHERE run_id = ? AND status = 'open'",
                (rid,),
            )

    async def mark_deleted(self, *, run_id: str, node_paths: Sequence[str]) -> int:
        """Mark nodes deleted (matched by sandbox or committed path).

        Returns how many rows transitioned. A step becomes ``deleted`` once
        every node it created is deleted. Idempotent: already-deleted nodes do
        not count.
        """
        rid = _require_run_id(run_id)
        paths = [p for p in node_paths if type(p) is str]
        if not paths:
            return 0
        count = 0
        async with self._database.write_transaction() as conn:
            for path in paths:
                cursor = await conn.execute(
                    "UPDATE task_nodes SET status = 'deleted' "
                    "WHERE run_id = ? AND status != 'deleted' "
                    "AND (node_path = ? OR committed_path = ?)",
                    (rid, path, path),
                )
                count += cursor.rowcount
            await conn.execute(
                "UPDATE task_steps SET status = 'deleted' WHERE run_id = ? "
                "AND status != 'deleted' AND step_id IN ("
                "  SELECT step_id FROM task_nodes GROUP BY step_id "
                "  HAVING SUM(status != 'deleted') = 0"
                ")",
                (rid,),
            )
        return count

    async def list_run_steps(self, run_id: str) -> tuple[TaskStep, ...]:
        """All steps of a run (oldest first) with their nodes attached."""
        rid = _require_run_id(run_id)
        step_rows = await self._database.fetchall(
            f"SELECT {_STEP_COLUMNS} FROM task_steps WHERE run_id = ? ORDER BY seq",
            (rid,),
        )
        node_rows = await self._database.fetchall(
            f"SELECT {_NODE_COLUMNS} FROM task_nodes WHERE run_id = ? ORDER BY node_id",
            (rid,),
        )
        by_step: dict[int, list[TaskNode]] = {}
        for row in node_rows:
            by_step.setdefault(row["step_id"], []).append(
                TaskNode(
                    node_id=row["node_id"],
                    step_id=row["step_id"],
                    node_path=row["node_path"],
                    committed_path=row["committed_path"],
                    node_type=row["node_type"],
                    note=row["note"],
                    status=row["status"],
                )
            )
        return tuple(
            TaskStep(
                step_id=row["step_id"],
                run_id=row["run_id"],
                seq=row["seq"],
                tool=row["tool"],
                purpose=row["purpose"],
                status=row["status"],
                created_at=datetime.fromisoformat(row["created_at"]),
                nodes=tuple(by_step.get(row["step_id"], ())),
            )
            for row in step_rows
        )

    async def render_summary(self, run_id: str) -> str | None:
        """Compact human/LLM summary: last 10 steps, <= 2KB UTF-8.

        Returns ``None`` when the run has no recorded steps (the caller then
        skips injection entirely).
        """
        rid = _require_run_id(run_id)
        steps = await self.list_run_steps(rid)
        if not steps:
            return None
        deleted_count = sum(
            1 for step in steps for node in step.nodes if node.status == "deleted"
        )
        lines = [f"Task state (run {rid}):"]
        shown = steps[-_SUMMARY_MAX_STEPS:]
        omitted = len(steps) - len(shown)
        if omitted:
            lines.append(f"({omitted} earlier step(s) omitted)")
        for step in shown:
            names = ", ".join(
                (node.committed_path or node.node_path).rsplit("/", 1)[-1]
                for node in step.nodes
                if node.status != "deleted"
            )
            suffix = f" ({names})" if names else ""
            lines.append(f"{step.seq}. [{step.status}] {step.purpose}{suffix}")
        current = self._current_output(steps)
        if current is not None:
            lines.append(f"Current output: {current}")
        if deleted_count:
            lines.append(f"Cleaned up nodes: {deleted_count}")
        # Byte cap: drop the oldest remaining step line until the block fits.
        first_step_line = 2 if omitted else 1
        while (
            len("\n".join(lines).encode("utf-8")) > _SUMMARY_MAX_BYTES
            and len(lines) > first_step_line + 1
        ):
            del lines[first_step_line]
        text = "\n".join(lines)
        encoded = text.encode("utf-8")
        if len(encoded) > _SUMMARY_MAX_BYTES:
            text = encoded[:_SUMMARY_MAX_BYTES].decode("utf-8", errors="ignore")
        return text

    @staticmethod
    def _current_output(steps: tuple[TaskStep, ...]) -> str | None:
        """The committed path of the most recently committed node, if any."""
        for step in reversed(steps):
            for node in reversed(step.nodes):
                if node.status == "committed" and node.committed_path:
                    return node.committed_path
        return None


@dataclass(frozen=True, slots=True)
class TaskGraphToolContext:
    """Per-run task graph handle injected into the Runtime tool context."""

    store: TaskGraphStore
    run_id: str

    def __post_init__(self) -> None:
        if type(self.store) is not TaskGraphStore:
            raise TypeError("TaskGraphToolContext.store must be a TaskGraphStore")
        if type(self.run_id) is not str or not self.run_id:
            raise TypeError("TaskGraphToolContext.run_id must be a non-empty string")


__all__ = [
    "TaskGraphStore",
    "TaskGraphToolContext",
    "TaskNode",
    "TaskStep",
]
```

- [ ] **Step 1.5 — Update the stale schema-version assertions.** In `tests/runtime/test_database.py`, change every hard-coded `== 7` schema assertion to `== 8`, the table-set assertion in `test_empty_database_reaches_schema_v4` to include `"task_steps"` and `"task_nodes"`, and the applied-version list `[1, 2, 3, 4, 5, 6, 7]` to `[1, 2, 3, 4, 5, 6, 7, 8]` (in `test_reopen_does_not_rerun_migration` and any other version-list test). Do not rename the stale test functions — out of scope.

- [ ] **Step 1.6 — Run the store + database tests; expect pass.** `uv run --frozen --extra eval pytest -q tests/runtime/test_task_graph.py tests/runtime/test_database.py` → all pass.

- [ ] **Step 1.7 — Commit.**
  `git add eee_agent/runtime/migrations.py eee_agent/runtime/task_graph.py tests/runtime/test_task_graph.py tests/runtime/test_database.py`
  `git commit -m "feat(runtime): task graph store + schema v8 (task_steps/task_nodes)"`

---

## Task 2 — `scratch.py` DTO extensions (purpose / note / annotations / warnings / delete_node)

**Files:**
- Modify: `eee_agent/houdini_bridge/scratch.py` (constants lines 61-92, `ScratchOp` lines 171-250, `ScratchRequest` lines 311-417, `_COMMIT_PAYLOAD_FIELDS` lines 562-568, `ScratchCommitRequest` lines 606-718, `ScratchCommitResult` lines 721-785)
- Modify: `eee_agent/houdini_bridge/changeset_provider.py` (`scratch_exec` lines 214-237, `scratch_commit` lines 239-267)
- Modify: `eee_agent/modeling/scratch_coordinator.py` (provider Protocol lines 57-91, `ScratchCoordinator.build` lines 128-150, `commit` lines 152-197, `_summarize_commit`, `scratch_build` tool lines 819-921) — only the `purpose`/`annotations` threading needed to keep the build green; store recording is Task 6.
- Test: `tests/runtime/test_scratch_bridge.py` (modify)

- [ ] **Step 2.1 — Update existing tests that will break, and add the new failing DTO tests.** In `tests/runtime/test_scratch_bridge.py`:
  - Change `test_unknown_kind_rejected` (lines 207-209) to use a still-unknown kind:
    ```python
    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchOp(kind="move_node", node_name="x")
    ```
  - Update the `_scratch_request()` helper (lines 526-533) to pass `purpose="build the tabletop"` to `ScratchRequest(...)`.
  - Update every other direct `ScratchRequest(` / `ScratchRequest.build(` construction in the file to include a valid `purpose`, and every `_FakeScratchProvider.scratch_exec` fake signature to accept `purpose: str` (record it in `self.calls`).
  - Add a new test class:

```python
class TestScratchV2DtoExtensions:
    """scratch_build purpose, create_node note, commit annotations/warnings,
    and the delete_node op kind."""

    def test_delete_node_round_trip(self) -> None:
        op = ScratchOp(kind="delete_node", node_name="draft1")
        d = op.to_dict()
        assert d == {"kind": "delete_node", "node_name": "draft1"}
        assert ScratchOp.from_dict(d) == op

    def test_create_node_with_note_round_trip(self) -> None:
        op = ScratchOp(
            kind="create_node", node_name="box1", node_type="box", note="桌面粗模"
        )
        d = op.to_dict()
        assert d["note"] == "桌面粗模"
        assert ScratchOp.from_dict(d) == op

    def test_note_rejected_on_other_kinds(self) -> None:
        with pytest.raises(ValueError):
            ScratchOp(kind="delete_node", node_name="x", note="nope")

    def test_note_over_200_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchOp(
                kind="create_node", node_name="box1", node_type="box",
                note="x" * 201,
            )

    def test_purpose_round_trip(self) -> None:
        req = ScratchRequest(
            request_id="req_p1",
            deadline_ms=5000,
            scene_epoch=1,
            sandbox_id="run1",
            operations=(ScratchOp(kind="create_node", node_name="b", node_type="box"),),
            purpose="创建桌腿",
        )
        parsed = ScratchRequest.from_dict(req.to_dict())
        assert parsed.purpose == "创建桌腿"

    def test_purpose_required_bounded(self) -> None:
        base = dict(
            request_id="req_p2",
            deadline_ms=5000,
            scene_epoch=1,
            sandbox_id="run1",
            operations=(ScratchOp(kind="create_node", node_name="b", node_type="box"),),
        )
        with pytest.raises(ValueError):
            ScratchRequest(**base, purpose="")
        with pytest.raises(ValueError):
            ScratchRequest(**base, purpose="x" * 201)

    def test_annotations_round_trip(self) -> None:
        req = ScratchCommitRequest.build(
            request_id="req_c1",
            deadline_ms=5000,
            scene_epoch=1,
            sandbox_id="run1",
            target_parent_path="/obj",
            target_name="table1",
            annotations={"box1": "桌面", "blast1": "最终输出"},
        )
        parsed = ScratchCommitRequest.from_dict(req.to_dict())
        assert dict(parsed.annotations) == {"box1": "桌面", "blast1": "最终输出"}

    def test_annotations_validation(self) -> None:
        with pytest.raises(ValueError):
            ScratchCommitRequest.build(
                request_id="req_c2",
                deadline_ms=5000,
                scene_epoch=1,
                sandbox_id="run1",
                target_parent_path="/obj",
                target_name="table1",
                annotations={"not a name!": "x"},
            )
        with pytest.raises(ValueError):
            ScratchCommitRequest.build(
                request_id="req_c3",
                deadline_ms=5000,
                scene_epoch=1,
                sandbox_id="run1",
                target_parent_path="/obj",
                target_name="table1",
                annotations={"box1": "x" * 501},
            )

    def test_commit_result_warnings_round_trip(self) -> None:
        result = ScratchCommitResult(
            committed=True,
            refused=False,
            final_path="/obj/table1",
            reason="",
            gates=(),
            receipt={"passed": True},
            warnings=("layout skipped: boom",),
        )
        parsed = ScratchCommitResult.from_dict(result.to_dict())
        assert parsed.warnings == ("layout skipped: boom",)
```

- [ ] **Step 2.2 — Run the DTO tests; expect failures.** `uv run --frozen --extra eval pytest -q tests/runtime/test_scratch_bridge.py` → new tests fail (unknown field `purpose`/`note`/`annotations`/`warnings`, `delete_node` rejected).

- [ ] **Step 2.3 — Implement the DTO changes in `eee_agent/houdini_bridge/scratch.py`.**

  Constants (after `_MAX_SANDBOX_NAME_LEN = 128` line):

```python
_MAX_PURPOSE_CHARS = 200
_MAX_NOTE_CHARS = 200
_MAX_ANNOTATION_CHARS = 500
```

  `_OP_FIELDS` gains `"note"`; `_OP_KINDS` becomes:

```python
_OP_KINDS = frozenset({"create_node", "set_parm", "connect", "delete_node"})
```

  New helper next to `_require_error_text`:

```python
def _require_bounded_text(value: object, label: str, max_len: int) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value or len(value) > max_len:
        raise ValueError(f"{label} must be 1..{max_len} characters")
```

  `ScratchOp`: add field `note: str = ""` (after `source_output_index`); in `__post_init__` append:

```python
        if self.note:
            if self.kind != "create_node":
                raise ValueError("ScratchOp.note is only valid for create_node")
            _require_bounded_text(self.note, "ScratchOp.note", _MAX_NOTE_CHARS)
```

  `from_dict` kwargs add `note=d.get("note", ""),  # type: ignore[arg-type]`; `to_dict` create_node branch adds:

```python
            if self.note:
                d["note"] = self.note
```

  `ScratchRequest`: add `purpose: str` field between `operations` and `preserve_on_failure`; `__post_init__` adds `_require_bounded_text(self.purpose, "ScratchRequest.purpose", _MAX_PURPOSE_CHARS)`; `_PAYLOAD_FIELDS` gains `"purpose"`; `build()` gains a required `purpose: str` keyword; `to_dict` payload gains `"purpose": self.purpose`; `from_dict` passes `purpose=payload["purpose"]`.

  `ScratchCommitRequest`: `_COMMIT_PAYLOAD_FIELDS` gains `"annotations"`. Add field (after `skip_structure_check`):

```python
    annotations: tuple[tuple[str, str], ...] = ()
```

  `__post_init__` appends:

```python
        annotations = tuple(self.annotations)
        if len(annotations) > _MAX_OPS:
            raise ValueError("ScratchCommitRequest.annotations exceeds the maximum count")
        for name, comment in annotations:
            _require_node_name(name, "ScratchCommitRequest.annotations key")
            if type(comment) is not str or not comment or len(comment) > _MAX_ANNOTATION_CHARS:
                raise ValueError(
                    "ScratchCommitRequest.annotations values must be "
                    f"1..{_MAX_ANNOTATION_CHARS} characters"
                )
        object.__setattr__(self, "annotations", annotations)
```

  `build()` gains `annotations: Mapping[str, str] | None = None` and passes `annotations=tuple((k, v) for k, v in (annotations or {}).items())`; `to_dict` payload gains `"annotations": {k: v for k, v in self.annotations}`; `from_dict` validates `type(payload["annotations"]) is dict` and passes `annotations=tuple((str(k), v) for k, v in payload["annotations"].items())`.

  `ScratchCommitResult`: `_COMMIT_RESULT_FIELDS` gains `"warnings"`. Add field `warnings: tuple[str, ...] = ()`; `__post_init__` appends:

```python
        warnings = tuple(self.warnings)
        if len(warnings) > _MAX_ERRORS:
            raise ValueError("ScratchCommitResult.warnings exceeds the maximum count")
        for w in warnings:
            _require_error_text(w, "ScratchCommitResult.warnings")
        object.__setattr__(self, "warnings", warnings)
```

  `to_dict` gains `"warnings": list(self.warnings)`; `from_dict` passes `warnings=tuple(d["warnings"])`.

- [ ] **Step 2.4 — Thread the new fields through the provider and coordinator (minimal, to keep the gate green).**
  - `eee_agent/houdini_bridge/changeset_provider.py` `scratch_exec`: add required keyword `purpose: str` and pass `purpose=purpose` to `ScratchRequest.build`. `scratch_commit`: add keyword `annotations: Mapping[str, str] | None = None` and pass `annotations=annotations` to `ScratchCommitRequest.build`.
  - `eee_agent/modeling/scratch_coordinator.py`:
    - `ScratchProvider.scratch_exec` Protocol signature gains `purpose: str`; `scratch_commit` gains `annotations: tuple[tuple[str, str], ...] = ()`.
    - `ScratchCoordinator.build` gains a required keyword `purpose: str`, validates it (`type(purpose) is not str or not purpose.strip() or len(purpose) > 200` → `ScratchError("scratch.input_invalid", "purpose must be 1..200 characters.")`), and passes `purpose=purpose` to the provider call.
    - `ScratchCoordinator.commit` passes `annotations=()` to the provider call for now (Task 6 replaces this with graph-derived annotations).
    - `_summarize_commit` adds `"warnings": list(result.warnings),` to the returned dict.
    - The `scratch_build` tool signature becomes:

      ```python
      @tool
      async def scratch_build(
          purpose: str,
          operations: list[dict[str, object]],
          runtime: ToolRuntime,
          preserve_on_failure: bool = True,
      ) -> dict[str, object]:
      ```

      Add a `purpose` entry to the docstring (`purpose: one sentence (<=200 chars) saying WHAT this batch builds and WHY — it is recorded in the run's task graph and later used for cleanup and scene comments.`), validate it at the tool seam like `preserve_on_failure`:

      ```python
          if type(purpose) is not str or not purpose.strip() or len(purpose) > 200:
              return {
                  "ok": False,
                  "code": "scratch.input_invalid",
                  "message": "purpose must be 1..200 characters.",
              }
      ```

      and pass `purpose=purpose` into `coordinator.build(...)`.

- [ ] **Step 2.5 — Update remaining call sites and fakes so the suite compiles.** In `tests/runtime/test_scratch_bridge.py` make `_FakeScratchProvider.scratch_exec` accept/record `purpose`, its `scratch_commit` accept `annotations`, every `coord.build(...)` call pass `purpose="..."`, and every `scratch_build.coroutine(...)` tool-invocation call (the `TestScratchBuildTool` class) pass a valid `purpose="..."` kwarg (the tool's first parameter). Any other test constructing `ScratchCommitResult(` directly adds `warnings=()` or relies on the default.

- [ ] **Step 2.6 — Run the full bridge + coordinator tests; expect pass.** `uv run --frozen --extra eval pytest -q tests/runtime/test_scratch_bridge.py` → all pass. Then run the wider offline gate slice: `uv run --frozen --extra eval pytest -q tests/runtime tests/modeling` → all pass.

- [ ] **Step 2.7 — Commit.**
  `git add eee_agent/houdini_bridge/scratch.py eee_agent/houdini_bridge/changeset_provider.py eee_agent/modeling/scratch_coordinator.py tests/runtime/test_scratch_bridge.py`
  `git commit -m "feat(scratch): purpose/note/annotations/warnings DTO fields + delete_node op kind"`

---

## Task 3 — Bridge plumbing: `scratch.v2` capability, `scratch.delete` + `scratch.topology` ops, dispatch

**Files:**
- Modify: `eee_agent/houdini_bridge/scratch.py` (constants + new DTO section before the entrypoints at line 1057)
- Modify: `eee_agent/houdini_bridge/client.py` (`scratch_exec` line 689-745 delete-op gate; new methods after `scratch_destroy` line 808-864)
- Modify: `eee_agent/houdini_bridge/changeset_provider.py` (new methods after `scratch_destroy` line 269-287)
- Modify: `houdini_side/secure_bridge.py` (default capabilities lines 538-541; dispatch after the `SCRATCH_DESTROY_OPERATION` arm lines 879-893; new handlers after `_serve_scratch_destroy` line 1334-1374; delete-op admission in `_serve_scratch_exec` lines 1242-1250)
- Test: `tests/runtime/test_scratch_bridge.py` (modify — add delete/topology DTO + client tests)

- [ ] **Step 3.1 — Write the failing tests.** Append to `tests/runtime/test_scratch_bridge.py`:

```python
class TestScratchDeleteTopologyDtos:
    def test_delete_request_round_trip(self) -> None:
        req = ScratchDeleteRequest.build(
            request_id="req_d1",
            deadline_ms=5000,
            scene_epoch=1,
            allowed_paths=("/obj/table1/box1", "/obj/table1/draft1"),
            paths=("/obj/table1/draft1",),
        )
        parsed = ScratchDeleteRequest.from_dict(req.to_dict())
        assert parsed.paths == ("/obj/table1/draft1",)
        assert parsed.allowed_paths == ("/obj/table1/box1", "/obj/table1/draft1")

    def test_delete_request_paths_must_be_allowlisted(self) -> None:
        with pytest.raises(ValueError):
            ScratchDeleteRequest.build(
                request_id="req_d2",
                deadline_ms=5000,
                scene_epoch=1,
                allowed_paths=("/obj/table1/box1",),
                paths=("/obj/other/nope",),
            )

    def test_delete_result_round_trip(self) -> None:
        result = ScratchDeleteResult(
            deleted_paths=("/obj/table1/draft1",),
            skipped=({"path": "/obj/table1/box1", "reason": "still referenced by /obj/table1/out"},),
        )
        parsed = ScratchDeleteResult.from_dict(result.to_dict())
        assert parsed.deleted_paths == ("/obj/table1/draft1",)
        assert parsed.skipped[0]["path"] == "/obj/table1/box1"

    def test_topology_request_round_trip(self) -> None:
        req = ScratchTopologyRequest.build(
            request_id="req_t1",
            deadline_ms=5000,
            scene_epoch=1,
            paths=("/obj/table1/box1",),
        )
        parsed = ScratchTopologyRequest.from_dict(req.to_dict())
        assert parsed.paths == ("/obj/table1/box1",)

    def test_topology_result_round_trip(self) -> None:
        result = ScratchTopologyResult(
            nodes=(
                {"path": "/obj/table1/box1", "exists": True,
                 "inputs": [], "outputs": ["/obj/table1/xform1"],
                 "display_flag": False},
                {"path": "/obj/table1/gone", "exists": False,
                 "inputs": [], "outputs": [], "display_flag": False},
            ),
        )
        parsed = ScratchTopologyResult.from_dict(result.to_dict())
        assert parsed.nodes[0]["outputs"] == ["/obj/table1/xform1"]
        assert parsed.nodes[1]["exists"] is False


def _delete_request() -> ScratchDeleteRequest:
    return ScratchDeleteRequest.build(
        request_id="req_del_001",
        deadline_ms=5000,
        scene_epoch=42,
        allowed_paths=("/obj/table1/draft1",),
        paths=("/obj/table1/draft1",),
    )


def _topology_request() -> ScratchTopologyRequest:
    return ScratchTopologyRequest.build(
        request_id="req_topo_001",
        deadline_ms=5000,
        scene_epoch=42,
        paths=("/obj/table1/draft1",),
    )


@async_test
async def test_scratch_delete_without_v2_capability_sends_no_frame() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=True, caps=["scratch.v1"]))
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_delete_nodes(_delete_request())
    assert exc.value.code == "bridge.capability_unavailable"
    assert len(_parse_frames(bytes(fake.outbox))) == 1


@async_test
async def test_scratch_topology_without_v2_capability_sends_no_frame() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=True, caps=["scratch.v1"]))
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_topology(_topology_request())
    assert exc.value.code == "bridge.capability_unavailable"
    assert len(_parse_frames(bytes(fake.outbox))) == 1


@async_test
async def test_scratch_exec_delete_op_requires_v2_capability() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=True, caps=["scratch.v1"]))
    client = _client(fake)
    await client.open()
    request = ScratchRequest(
        request_id="req_scratch_del",
        deadline_ms=5000,
        scene_epoch=42,
        sandbox_id="run1",
        operations=(ScratchOp(kind="delete_node", node_name="draft1"),),
        purpose="cleanup draft",
    )
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_exec(request)
    assert exc.value.code == "bridge.capability_unavailable"
    assert len(_parse_frames(bytes(fake.outbox))) == 1


@async_test
async def test_scratch_delete_happy_path() -> None:
    result_payload = {
        "deleted_paths": ["/obj/table1/draft1"],
        "skipped": [],
    }
    response = {
        "protocol": _PROTO,
        "kind": "response",
        "request_id": "req_del_001",
        "ok": True,
        "result": result_payload,
    }
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=["scratch.v1", "scratch.v2"]) + _frame(_dumps(response))
    )
    client = _client(fake)
    await client.open()
    result = await client.scratch_delete_nodes(_delete_request())
    assert result.deleted_paths == ("/obj/table1/draft1",)
    assert result.skipped == ()
```

(If `_PROTO`/`_dumps` are named differently in the file, reuse whatever the existing client tests use.)

- [ ] **Step 3.2 — Run; expect import/attribute failures.** `uv run --frozen --extra eval pytest -q tests/runtime/test_scratch_bridge.py -k "Delete or Topology or delete or topology"` → fails (`ImportError: cannot import name 'ScratchDeleteRequest'`).

- [ ] **Step 3.3 — Add the `scratch.v2` DTOs to `eee_agent/houdini_bridge/scratch.py`.** Constants (next to `SCRATCH_DESTROY_OPERATION`):

```python
SCRATCH_V2 = "scratch.v2"
SCRATCH_DELETE_OPERATION = "scratch.delete"
SCRATCH_TOPOLOGY_OPERATION = "scratch.topology"
```

New section before the `entrypoints` comment:

```python
# --------------------------------------------------------------------------
# delete + topology DTOs (scratch.v2 — node cleanup surfaces)
# --------------------------------------------------------------------------

_DELETE_PAYLOAD_FIELDS = frozenset({"allowed_paths", "paths"})
_DELETE_RESULT_FIELDS = frozenset({"deleted_paths", "skipped"})
_SKIPPED_FIELDS = frozenset({"path", "reason"})
_TOPOLOGY_PAYLOAD_FIELDS = frozenset({"paths"})
_TOPOLOGY_RESULT_FIELDS = frozenset({"nodes"})
_TOPOLOGY_NODE_FIELDS = frozenset(
    {"path", "exists", "inputs", "outputs", "display_flag"}
)
_MAX_DELETE_PATHS = 64
_MAX_TOPOLOGY_PATHS = 64
_MAX_TOPOLOGY_EDGES = 64


def _require_node_path_list(value: object, label: str, max_count: int) -> tuple[str, ...]:
    if type(value) is not list and type(value) is not tuple:
        raise TypeError(f"{label} must be a list")
    paths = tuple(value)
    if not paths or len(paths) > max_count:
        raise ValueError(f"{label} must contain 1..{max_count} paths")
    for path in paths:
        _require_node_path(path, label)
    return paths


def _require_skipped_entry(value: object) -> dict[str, object]:
    d = _require_exact_dict(value, "ScratchDeleteResult.skipped entry")
    _require_exact_keys(d, _SKIPPED_FIELDS, "ScratchDeleteResult.skipped entry")
    _require_node_path(d["path"], "ScratchDeleteResult.skipped path")
    _require_error_text(d["reason"], "ScratchDeleteResult.skipped reason")
    return {"path": d["path"], "reason": d["reason"]}


def _require_topology_entry(value: object) -> dict[str, object]:
    d = _require_exact_dict(value, "ScratchTopologyResult entry")
    _require_exact_keys(d, _TOPOLOGY_NODE_FIELDS, "ScratchTopologyResult entry")
    _require_node_path(d["path"], "ScratchTopologyResult path")
    _require_exact_bool(d["exists"], "ScratchTopologyResult exists")
    _require_exact_bool(d["display_flag"], "ScratchTopologyResult display_flag")
    for field in ("inputs", "outputs"):
        edges = d[field]
        if type(edges) is not list or len(edges) > _MAX_TOPOLOGY_EDGES:
            raise ValueError(f"ScratchTopologyResult {field} must be a bounded list")
        for edge in edges:
            _require_node_path(edge, f"ScratchTopologyResult {field}")
    return {
        "path": d["path"],
        "exists": d["exists"],
        "inputs": list(d["inputs"]),
        "outputs": list(d["outputs"]),
        "display_flag": d["display_flag"],
    }


@dataclass(frozen=True, slots=True)
class ScratchDeleteRequest:
    """A parsed, validated ``scratch.delete`` request envelope.

    Deletes committed scene nodes, but ONLY paths the Runtime explicitly
    allowlisted for deletion (from its task graph): the Houdini side never
    decides what may be deleted. ``paths`` must be a subset of
    ``allowed_paths`` — a request that names anything else fails closed at
    parse time.
    """

    request_id: str
    deadline_ms: int
    scene_epoch: int
    allowed_paths: tuple[str, ...]
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "ScratchDeleteRequest.request_id")
        _require_exact_int(self.deadline_ms, "ScratchDeleteRequest.deadline_ms")
        if self.deadline_ms < _MIN_DEADLINE_MS or self.deadline_ms > _MAX_DEADLINE_MS:
            raise ValueError("ScratchDeleteRequest.deadline_ms must be in 1..30000")
        _require_exact_int(self.scene_epoch, "ScratchDeleteRequest.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("ScratchDeleteRequest.scene_epoch must be >= 1")
        allowed = _require_node_path_list(
            list(self.allowed_paths), "ScratchDeleteRequest.allowed_paths", 256
        )
        paths = _require_node_path_list(
            list(self.paths), "ScratchDeleteRequest.paths", _MAX_DELETE_PATHS
        )
        if not set(paths) <= set(allowed):
            raise ValueError("ScratchDeleteRequest.paths must be a subset of allowed_paths")
        object.__setattr__(self, "allowed_paths", allowed)
        object.__setattr__(self, "paths", paths)

    @classmethod
    def build(
        cls,
        *,
        request_id: str,
        deadline_ms: int,
        scene_epoch: int,
        allowed_paths: Sequence[str],
        paths: Sequence[str],
    ) -> "ScratchDeleteRequest":
        return cls(
            request_id=request_id,
            deadline_ms=deadline_ms,
            scene_epoch=scene_epoch,
            allowed_paths=tuple(allowed_paths),
            paths=tuple(paths),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": self.request_id,
            "operation": SCRATCH_DELETE_OPERATION,
            "deadline_ms": self.deadline_ms,
            "scene_epoch": self.scene_epoch,
            "payload": {
                "allowed_paths": list(self.allowed_paths),
                "paths": list(self.paths),
            },
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchDeleteRequest":
        envelope = _require_exact_dict(data, "ScratchDeleteRequest envelope")
        _require_exact_keys(envelope, _REQUEST_FIELDS, "ScratchDeleteRequest envelope")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("ScratchDeleteRequest protocol must be eee.bridge/1")
        if envelope["kind"] != "request":
            raise ValueError("ScratchDeleteRequest kind must be request")
        if envelope["operation"] != SCRATCH_DELETE_OPERATION:
            raise ValueError("ScratchDeleteRequest operation must be scratch.delete")
        payload = _require_exact_dict(envelope["payload"], "ScratchDeleteRequest payload")
        _require_exact_keys(payload, _DELETE_PAYLOAD_FIELDS, "ScratchDeleteRequest payload")
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            deadline_ms=envelope["deadline_ms"],  # type: ignore[arg-type]
            scene_epoch=envelope["scene_epoch"],  # type: ignore[arg-type]
            allowed_paths=tuple(payload["allowed_paths"]),  # type: ignore[arg-type]
            paths=tuple(payload["paths"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ScratchDeleteResult:
    """The bounded result of a ``scratch.delete`` call (per-path verdicts)."""

    deleted_paths: tuple[str, ...]
    skipped: tuple[dict[str, object], ...]

    def __post_init__(self) -> None:
        paths = tuple(self.deleted_paths)
        if len(paths) > _MAX_DELETE_PATHS:
            raise ValueError("ScratchDeleteResult.deleted_paths exceeds the maximum count")
        for p in paths:
            _require_node_path(p, "ScratchDeleteResult.deleted_paths")
        object.__setattr__(self, "deleted_paths", paths)
        skipped = tuple(self.skipped)
        if len(skipped) > _MAX_DELETE_PATHS:
            raise ValueError("ScratchDeleteResult.skipped exceeds the maximum count")
        object.__setattr__(
            self, "skipped", tuple(_require_skipped_entry(s) for s in skipped)
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchDeleteResult":
        d = _require_exact_dict(data, "ScratchDeleteResult")
        _require_exact_keys(d, _DELETE_RESULT_FIELDS, "ScratchDeleteResult")
        if type(d["deleted_paths"]) is not list or type(d["skipped"]) is not list:
            raise TypeError("ScratchDeleteResult fields must be lists")
        return cls(
            deleted_paths=tuple(d["deleted_paths"]),  # type: ignore[arg-type]
            skipped=tuple(dict(s) for s in d["skipped"]),  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "deleted_paths": list(self.deleted_paths),
            "skipped": [dict(s) for s in self.skipped],
        }


@dataclass(frozen=True, slots=True)
class ScratchDeleteResponse:
    """The ``scratch.delete`` response envelope (mirrors ScratchResponse)."""

    request_id: str
    result: ScratchDeleteResult | None
    error: BridgeError | None

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "ScratchDeleteResponse.request_id")
        if (self.result is None) == (self.error is None):
            raise ValueError("ScratchDeleteResponse must carry exactly one of result/error")
        if self.error is not None and type(self.error) is not BridgeError:
            raise TypeError("ScratchDeleteResponse.error must be an exact BridgeError")

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "protocol": PROTOCOL,
            "kind": "response",
            "request_id": self.request_id,
            "ok": self.result is not None,
        }
        if self.result is not None:
            d["result"] = self.result.to_dict()
        if self.error is not None:
            d["error"] = self.error.to_dict()
        return d

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchDeleteResponse":
        envelope = _require_exact_dict(data, "ScratchDeleteResponse envelope")
        if not _RESPONSE_REQUIRED_FIELDS.issubset(envelope.keys()):
            raise ValueError("ScratchDeleteResponse envelope is missing required fields")
        extra = set(envelope.keys()) - _RESPONSE_REQUIRED_FIELDS - {"result", "error"}
        if extra:
            raise ValueError("ScratchDeleteResponse envelope has unknown fields")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("ScratchDeleteResponse protocol must be eee.bridge/1")
        if envelope["kind"] != "response":
            raise ValueError("ScratchDeleteResponse kind must be response")
        ok = envelope["ok"]
        _require_exact_bool(ok, "ScratchDeleteResponse.ok")
        if ok is True:
            result = envelope.get("result")
            error = envelope.get("error")
            if result is None or error is not None:
                raise ValueError("ScratchDeleteResponse ok=true requires result and no error")
            return cls(
                request_id=envelope["request_id"],  # type: ignore[arg-type]
                result=ScratchDeleteResult.from_dict(result),  # type: ignore[arg-type]
                error=None,
            )
        error = envelope.get("error")
        result = envelope.get("result")
        if error is None or result is not None:
            raise ValueError("ScratchDeleteResponse ok=false requires error and no result")
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            result=None,
            error=BridgeError.from_dict(error),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ScratchTopologyRequest:
    """A parsed, validated ``scratch.topology`` request envelope (read-only)."""

    request_id: str
    deadline_ms: int
    scene_epoch: int
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "ScratchTopologyRequest.request_id")
        _require_exact_int(self.deadline_ms, "ScratchTopologyRequest.deadline_ms")
        if self.deadline_ms < _MIN_DEADLINE_MS or self.deadline_ms > _MAX_DEADLINE_MS:
            raise ValueError("ScratchTopologyRequest.deadline_ms must be in 1..30000")
        _require_exact_int(self.scene_epoch, "ScratchTopologyRequest.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("ScratchTopologyRequest.scene_epoch must be >= 1")
        object.__setattr__(
            self,
            "paths",
            _require_node_path_list(
                list(self.paths), "ScratchTopologyRequest.paths", _MAX_TOPOLOGY_PATHS
            ),
        )

    @classmethod
    def build(
        cls,
        *,
        request_id: str,
        deadline_ms: int,
        scene_epoch: int,
        paths: Sequence[str],
    ) -> "ScratchTopologyRequest":
        return cls(
            request_id=request_id,
            deadline_ms=deadline_ms,
            scene_epoch=scene_epoch,
            paths=tuple(paths),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": self.request_id,
            "operation": SCRATCH_TOPOLOGY_OPERATION,
            "deadline_ms": self.deadline_ms,
            "scene_epoch": self.scene_epoch,
            "payload": {"paths": list(self.paths)},
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchTopologyRequest":
        envelope = _require_exact_dict(data, "ScratchTopologyRequest envelope")
        _require_exact_keys(envelope, _REQUEST_FIELDS, "ScratchTopologyRequest envelope")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("ScratchTopologyRequest protocol must be eee.bridge/1")
        if envelope["kind"] != "request":
            raise ValueError("ScratchTopologyRequest kind must be request")
        if envelope["operation"] != SCRATCH_TOPOLOGY_OPERATION:
            raise ValueError("ScratchTopologyRequest operation must be scratch.topology")
        payload = _require_exact_dict(envelope["payload"], "ScratchTopologyRequest payload")
        _require_exact_keys(payload, _TOPOLOGY_PAYLOAD_FIELDS, "ScratchTopologyRequest payload")
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            deadline_ms=envelope["deadline_ms"],  # type: ignore[arg-type]
            scene_epoch=envelope["scene_epoch"],  # type: ignore[arg-type]
            paths=tuple(payload["paths"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ScratchTopologyResult:
    """Per-path live wiring facts for cleanup decisions (bounded)."""

    nodes: tuple[dict[str, object], ...]

    def __post_init__(self) -> None:
        nodes = tuple(self.nodes)
        if len(nodes) > _MAX_TOPOLOGY_PATHS:
            raise ValueError("ScratchTopologyResult.nodes exceeds the maximum count")
        object.__setattr__(
            self, "nodes", tuple(_require_topology_entry(n) for n in nodes)
        )
        if len(canonical_json_dumps(self.to_dict())) > _MAX_RESULT_BYTES:
            raise ValueError("ScratchTopologyResult exceeds the maximum result size")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchTopologyResult":
        d = _require_exact_dict(data, "ScratchTopologyResult")
        _require_exact_keys(d, _TOPOLOGY_RESULT_FIELDS, "ScratchTopologyResult")
        if type(d["nodes"]) is not list:
            raise TypeError("ScratchTopologyResult nodes must be a list")
        return cls(nodes=tuple(dict(n) for n in d["nodes"]))  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        return {"nodes": [dict(n) for n in self.nodes]}


@dataclass(frozen=True, slots=True)
class ScratchTopologyResponse:
    """The ``scratch.topology`` response envelope (mirrors ScratchResponse)."""

    request_id: str
    result: ScratchTopologyResult | None
    error: BridgeError | None

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "ScratchTopologyResponse.request_id")
        if (self.result is None) == (self.error is None):
            raise ValueError("ScratchTopologyResponse must carry exactly one of result/error")
        if self.error is not None and type(self.error) is not BridgeError:
            raise TypeError("ScratchTopologyResponse.error must be an exact BridgeError")

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "protocol": PROTOCOL,
            "kind": "response",
            "request_id": self.request_id,
            "ok": self.result is not None,
        }
        if self.result is not None:
            d["result"] = self.result.to_dict()
        if self.error is not None:
            d["error"] = self.error.to_dict()
        return d

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchTopologyResponse":
        envelope = _require_exact_dict(data, "ScratchTopologyResponse envelope")
        if not _RESPONSE_REQUIRED_FIELDS.issubset(envelope.keys()):
            raise ValueError("ScratchTopologyResponse envelope is missing required fields")
        extra = set(envelope.keys()) - _RESPONSE_REQUIRED_FIELDS - {"result", "error"}
        if extra:
            raise ValueError("ScratchTopologyResponse envelope has unknown fields")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("ScratchTopologyResponse protocol must be eee.bridge/1")
        if envelope["kind"] != "response":
            raise ValueError("ScratchTopologyResponse kind must be response")
        ok = envelope["ok"]
        _require_exact_bool(ok, "ScratchTopologyResponse.ok")
        if ok is True:
            result = envelope.get("result")
            error = envelope.get("error")
            if result is None or error is not None:
                raise ValueError("ScratchTopologyResponse ok=true requires result and no error")
            return cls(
                request_id=envelope["request_id"],  # type: ignore[arg-type]
                result=ScratchTopologyResult.from_dict(result),  # type: ignore[arg-type]
                error=None,
            )
        error = envelope.get("error")
        result = envelope.get("result")
        if error is None or result is not None:
            raise ValueError("ScratchTopologyResponse ok=false requires error and no result")
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            result=None,
            error=BridgeError.from_dict(error),  # type: ignore[arg-type]
        )
```

  Entrypoints (next to the other parse functions):

```python
def parse_scratch_delete_request(raw: str | bytes) -> ScratchDeleteRequest:
    return ScratchDeleteRequest.from_dict(_load_strict_dict(raw, "Scratch delete request"))


def parse_scratch_delete_response(raw: str | bytes) -> ScratchDeleteResponse:
    return ScratchDeleteResponse.from_dict(_load_strict_dict(raw, "Scratch delete response"))


def parse_scratch_topology_request(raw: str | bytes) -> ScratchTopologyRequest:
    return ScratchTopologyRequest.from_dict(_load_strict_dict(raw, "Scratch topology request"))


def parse_scratch_topology_response(raw: str | bytes) -> ScratchTopologyResponse:
    return ScratchTopologyResponse.from_dict(_load_strict_dict(raw, "Scratch topology response"))
```

- [ ] **Step 3.4 — Client methods in `eee_agent/houdini_bridge/client.py`.** Add the new names to the scratch import block (line ~40): `SCRATCH_V2`, `ScratchDeleteRequest`, `ScratchDeleteResponse`, `ScratchDeleteResult`, `ScratchTopologyRequest`, `ScratchTopologyResponse`, `ScratchTopologyResult`, `parse_scratch_delete_response`, `parse_scratch_topology_response`. In `scratch_exec`, immediately after the `SCRATCH_V1` capability check, add:

```python
        if any(op.kind == "delete_node" for op in request.operations) and (
            SCRATCH_V2 not in self._capabilities
        ):
            raise _client_error(
                "bridge.capability_unavailable",
                "capability",
                "The bridge does not support scratch node deletion.",
                retryable=False,
            )
```

  Add after `scratch_destroy` (mirroring its structure exactly):

```python
    async def scratch_delete_nodes(
        self, request: ScratchDeleteRequest
    ) -> ScratchDeleteResult:
        """Send a ``scratch.delete`` request and return the per-path verdicts.

        Requires the advertised ``scratch.v2`` capability. The Houdini side
        deletes only allowlisted paths that are still unreferenced outside the
        delete set, inside one undoable group.
        """
        if type(request) is not ScratchDeleteRequest:
            raise TypeError("request must be a ScratchDeleteRequest")
        if SCRATCH_V2 not in self._capabilities:
            raise _client_error(
                "bridge.capability_unavailable",
                "capability",
                "The bridge does not support scratch node deletion.",
                retryable=False,
            )
        if not self._helloed or self._transport is None:
            raise RuntimeError(
                "BridgeClient.scratch_delete_nodes() requires a successful open()/hello"
            )
        response_bytes = await self._exchange(request.to_json(), request.deadline_ms)
        try:
            response = parse_scratch_delete_response(response_bytes)
        except (TypeError, ValueError) as exc:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge response is not a valid scratch delete envelope.",
            ) from exc
        if response.request_id != request.request_id:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge response does not match the request id.",
            )
        if response.error is not None:
            err = response.error
            raise BridgeClientError(
                code=err.code,
                category=err.category,
                message_for_user=err.message_for_user,
                retryable=err.retryable,
                technical_detail_ref=err.technical_detail_ref,
            )
        result = response.result
        if type(result) is not ScratchDeleteResult:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge scratch delete response is not a valid result.",
            )
        return result

    async def scratch_topology(
        self, request: ScratchTopologyRequest
    ) -> ScratchTopologyResult:
        """Send a ``scratch.topology`` request (read-only wiring facts).

        Requires the advertised ``scratch.v2`` capability. Never mutates the
        scene.
        """
        if type(request) is not ScratchTopologyRequest:
            raise TypeError("request must be a ScratchTopologyRequest")
        if SCRATCH_V2 not in self._capabilities:
            raise _client_error(
                "bridge.capability_unavailable",
                "capability",
                "The bridge does not support scratch topology queries.",
                retryable=False,
            )
        if not self._helloed or self._transport is None:
            raise RuntimeError(
                "BridgeClient.scratch_topology() requires a successful open()/hello"
            )
        response_bytes = await self._exchange(request.to_json(), request.deadline_ms)
        try:
            response = parse_scratch_topology_response(response_bytes)
        except (TypeError, ValueError) as exc:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge response is not a valid scratch topology envelope.",
            ) from exc
        if response.request_id != request.request_id:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge response does not match the request id.",
            )
        if response.error is not None:
            err = response.error
            raise BridgeClientError(
                code=err.code,
                category=err.category,
                message_for_user=err.message_for_user,
                retryable=err.retryable,
                technical_detail_ref=err.technical_detail_ref,
            )
        result = response.result
        if type(result) is not ScratchTopologyResult:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge scratch topology response is not a valid result.",
            )
        return result
```

- [ ] **Step 3.5 — Provider methods in `eee_agent/houdini_bridge/changeset_provider.py`.** Add after `scratch_destroy`:

```python
    async def delete_nodes(
        self,
        *,
        paths: tuple[str, ...],
        allowed_paths: tuple[str, ...],
    ) -> ScratchDeleteResult:
        """Delete committed nodes from a Runtime-supplied allowlist.

        The Houdini side never decides what may be deleted: ``paths`` must be
        a subset of ``allowed_paths`` (the Runtime's task-graph-derived
        allowlist). Any uncertainty raises with ``scene_may_have_changed=True``.
        """
        binding = await self.current_binding()
        request = ScratchDeleteRequest.build(
            request_id=self._request_id("scratch_delete"),
            deadline_ms=self._deadline_ms,
            scene_epoch=binding.scene_epoch,
            allowed_paths=allowed_paths,
            paths=paths,
        )
        return await self._call("scratch_delete_nodes", request, may_have_changed=True)

    async def scene_topology(
        self,
        *,
        paths: tuple[str, ...],
    ) -> ScratchTopologyResult:
        """Read-only per-path wiring facts for cleanup decisions."""
        binding = await self.current_binding()
        request = ScratchTopologyRequest.build(
            request_id=self._request_id("scratch_topology"),
            deadline_ms=self._deadline_ms,
            scene_epoch=binding.scene_epoch,
            paths=paths,
        )
        return await self._call("scratch_topology", request, may_have_changed=False)
```

  (Add the new DTO names to the scratch import block at the top of the file.)

- [ ] **Step 3.6 — Server dispatch in `houdini_side/secure_bridge.py`.**
  - Default capabilities (lines 538-541): add `SCRATCH_V2` after `SCRATCH_V1` (the tuple is sorted by `validate_capabilities`; `"scratch.v2"` sorts right after `"scratch.v1"`).
  - Import the new names from `eee_agent.houdini_bridge.scratch` alongside `SCRATCH_V1` (line ~75): `SCRATCH_V2`, `SCRATCH_DELETE_OPERATION`, `SCRATCH_TOPOLOGY_OPERATION`, `ScratchDeleteResponse`, `ScratchTopologyResponse`, `parse_scratch_delete_request`, `parse_scratch_topology_request`.
  - In `_serve_scratch_exec`, right after the successful parse (after `request_id = request.request_id`), add the admission check:

    ```python
        if any(op.kind == "delete_node" for op in request.operations) and (
            SCRATCH_V2 not in self._capabilities
        ):
            return self._error_envelope(
                request_id,
                code="bridge.capability_unavailable",
                category="capability",
                message_for_user="The bridge does not support scratch node deletion.",
            )
    ```

  - In `_serve`, after the `SCRATCH_DESTROY_OPERATION` arm (before the fall-through `bridge.invalid_request`), add:

    ```python
        if operation == SCRATCH_DELETE_OPERATION:
            # Admission: a server that does not advertise scratch.v2 must fail
            # closed BEFORE any HOM access or payload parsing.
            if SCRATCH_V2 not in self._capabilities:
                request_id = obj.get("request_id")
                if type(request_id) is not str:
                    request_id = _MALFORMED_REQUEST_ID
                return self._error_envelope(
                    request_id,
                    code="bridge.capability_unavailable",
                    category="capability",
                    message_for_user="The bridge does not support scratch node deletion.",
                )
            return await self._serve_scratch_delete(frame_bytes, reader=reader)
        if operation == SCRATCH_TOPOLOGY_OPERATION:
            # Read-only wiring facts; gated on scratch.v2 like delete.
            if SCRATCH_V2 not in self._capabilities:
                request_id = obj.get("request_id")
                if type(request_id) is not str:
                    request_id = _MALFORMED_REQUEST_ID
                return self._error_envelope(
                    request_id,
                    code="bridge.capability_unavailable",
                    category="capability",
                    message_for_user="The bridge does not support scratch topology queries.",
                )
            return await self._serve_scratch_topology(frame_bytes, reader=reader)
    ```

  - New handlers after `_serve_scratch_destroy`:

    ```python
    async def _serve_scratch_delete(
        self, frame_bytes: bytes, *, reader: object | None = None
    ) -> bytes:
        """Parse + queue a ``scratch.delete`` request; return per-path verdicts.

        Deletion IS a scene write, so it inherits the write-freeze gate. The
        executor only deletes allowlisted paths that are still unreferenced
        outside the delete set, inside one ``"EEE Agent - cleanup"`` undo group.
        """
        try:
            request = parse_scratch_delete_request(frame_bytes)
        except (TypeError, ValueError):
            return self._error_envelope(
                _MALFORMED_REQUEST_ID,
                code="bridge.invalid_request",
                category="protocol",
                message_for_user="The scratch delete request is not valid.",
            )
        request_id = request.request_id
        if self._executor.write_frozen:  # type: ignore[attr-defined]
            return self._error_envelope(
                request_id,
                code="bridge.write_frozen",
                category="write_frozen",
                message_for_user=(
                    "The bridge is frozen for writes after an uncertain recovery."
                ),
                retryable=False,
            )
        delete_request = request

        def operation() -> object:
            return self._executor.delete_nodes(delete_request)  # type: ignore[union-attr]

        result = await self._run_on_queue(
            request_id, request.deadline_ms, operation, reader=reader
        )
        if isinstance(result, _QueuedError):
            return self._error_envelope(
                request_id,
                code=result.code,
                category=result.category,
                message_for_user=result.message_for_user,
                retryable=result.retryable,
                technical_detail_ref=result.technical_detail_ref,
            )
        response = ScratchDeleteResponse(request_id=request_id, result=result, error=None)  # type: ignore[arg-type]
        return response.to_json().encode("utf-8")

    async def _serve_scratch_topology(
        self, frame_bytes: bytes, *, reader: object | None = None
    ) -> bytes:
        """Parse + queue a ``scratch.topology`` request; return wiring facts.

        Read-only: like ``scene.query`` it never touches the write-freeze gate
        and performs no mutation.
        """
        try:
            request = parse_scratch_topology_request(frame_bytes)
        except (TypeError, ValueError):
            return self._error_envelope(
                _MALFORMED_REQUEST_ID,
                code="bridge.invalid_request",
                category="protocol",
                message_for_user="The scratch topology request is not valid.",
            )
        request_id = request.request_id
        topology_request = request

        def operation() -> object:
            return self._executor.scratch_topology(topology_request)  # type: ignore[union-attr]

        result = await self._run_on_queue(
            request_id, request.deadline_ms, operation, reader=reader
        )
        if isinstance(result, _QueuedError):
            return self._error_envelope(
                request_id,
                code=result.code,
                category=result.category,
                message_for_user=result.message_for_user,
                retryable=result.retryable,
                technical_detail_ref=result.technical_detail_ref,
            )
        response = ScratchTopologyResponse(request_id=request_id, result=result, error=None)  # type: ignore[arg-type]
        return response.to_json().encode("utf-8")
    ```

- [ ] **Step 3.7 — Run; expect pass (client/DTO level).** `uv run --frozen --extra eval pytest -q tests/runtime/test_scratch_bridge.py` → all pass. (The executor methods `delete_nodes`/`scratch_topology` do not exist yet — they are only referenced inside Houdini-side handlers that the offline client tests never reach; they land in Task 5. Verify no offline test imports them missing by running `uv run --frozen --extra eval pytest -q tests/runtime tests/panel` → pass.)

- [ ] **Step 3.8 — Commit.**
  `git add eee_agent/houdini_bridge/scratch.py eee_agent/houdini_bridge/client.py eee_agent/houdini_bridge/changeset_provider.py houdini_side/secure_bridge.py tests/runtime/test_scratch_bridge.py`
  `git commit -m "feat(bridge): scratch.v2 capability with scratch.delete + scratch.topology ops"`

---

## Task 4 — Executor commit finalization: layered layout, display/render flags, comments

**Files:**
- Modify: `houdini_side/changeset_executor.py` (imports lines 41-47; constants near line 821-822; `scratch_commit` lines 2263-2328; new `_finalize_commit` method after `_scratch_output_node` line 2330-2343)
- Test: `tests/runtime/test_scratch_finalize.py` (create)

The offline seam is `_finalize_commit` called directly (a full offline `scratch_commit` would need the verify gates' cooked-geometry HOM surface, which the fake does not provide — the end-to-end path is covered by the Task 10 hython smoke). `_layered_layout` is a pure function tested in isolation.

- [ ] **Step 4.1 — Write the failing tests.** Create `tests/runtime/test_scratch_finalize.py`:

```python
"""Offline executor tests: commit finalization, delete_nodes, topology.

Drives :class:`ChangeSetExecutor` against a hou-free fake scene — no ``hou``,
no Houdini process. ``_layered_layout`` is pure Python and tested directly;
``_finalize_commit`` / ``delete_nodes`` / ``scratch_topology`` are called on a
real executor bound to the fake adapter. A full offline ``scratch_commit`` is
out of scope (the verify gates need a cooked-geometry HOM surface); the
end-to-end path is covered by ``tests/runtime/task_graph_houdini_smoke.py``.
"""

from __future__ import annotations

import contextlib

from eee_agent.houdini_bridge.scratch import (
    ScratchDeleteRequest,
    ScratchOp,
    ScratchRequest,
    ScratchTopologyRequest,
)
from houdini_side.changeset_executor import ChangeSetExecutor, _layered_layout
from houdini_side.secure_bridge import HoudiniSceneAdapter


# --------------------------------------------------------------------------
# hou-free fake scene
# --------------------------------------------------------------------------


class _Conn:
    def __init__(self, out_node: "_Node", out_idx: int, in_idx: int) -> None:
        self._out, self._oi, self._ii = out_node, out_idx, in_idx

    def outputNode(self) -> "_Node":
        return self._out

    def outputIndex(self) -> int:
        return self._oi

    def inputIndex(self) -> int:
        return self._ii


class _Category:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _Type:
    def __init__(self, name: str, category: str) -> None:
        self._name, self._category = name, category

    def name(self) -> str:
        return self._name

    def category(self) -> _Category:
        return _Category(self._category)


class _ParentRef:
    def __init__(self, path: str) -> None:
        self._path = path

    def path(self) -> str:
        return self._path


class _Node:
    def __init__(
        self,
        scene: dict[str, "_Node"],
        spy: list,
        path: str,
        type_name: str,
        parent: str,
        *,
        category: str = "Sop",
        fail_children: bool = False,
        fail_destroy: bool = False,
    ) -> None:
        self._scene, self._spy = scene, spy
        self._path, self._type_name, self._parent = path, type_name, parent
        self._category = category
        self._fail_children = fail_children
        self._fail_destroy = fail_destroy
        self._inputs: dict[int, tuple[_Node, int]] = {}
        self._outputs: list[_Node] = []
        self._children: list[_Node] = []
        self._position = (0.0, 0.0)
        self._display_flag = False
        self._render_flag = False
        self._comment = ""
        self._generic_flags: dict[object, bool] = {}

    def _record(self, method: str, *args: object) -> None:
        self._spy.append((method, self._path, *args))

    def path(self) -> str:
        return self._path

    def name(self) -> str:
        return self._path.rsplit("/", 1)[-1]

    def type(self) -> _Type:
        return _Type(self._type_name, self._category)

    def parent(self) -> _ParentRef:
        return _ParentRef(self._parent)

    def children(self) -> list["_Node"]:
        if self._fail_children:
            raise RuntimeError("children read failed")
        return list(self._children)

    def position(self) -> tuple[float, float]:
        return self._position

    def setPosition(self, pos: object) -> None:
        self._record("setPosition", tuple(pos))  # type: ignore[arg-type]
        self._position = (float(pos[0]), float(pos[1]))  # type: ignore[index]

    def setDisplayFlag(self, on: bool) -> None:
        self._record("setDisplayFlag", on)
        self._display_flag = bool(on)

    def isDisplayFlagSet(self) -> bool:
        return self._display_flag

    def setRenderFlag(self, on: bool) -> None:
        self._record("setRenderFlag", on)
        self._render_flag = bool(on)

    def setComment(self, text: str) -> None:
        self._record("setComment", text)
        self._comment = text

    def setGenericFlag(self, flag: object, on: bool) -> None:
        self._record("setGenericFlag", flag, on)
        self._generic_flags[flag] = bool(on)

    def inputConnections(self) -> list[_Conn]:
        return [
            _Conn(self._inputs[i][0], self._inputs[i][1], i)
            for i in sorted(self._inputs)
        ]

    def outputs(self) -> list["_Node"]:
        return list(self._outputs)

    def setInput(self, idx: int, src: "_Node | None", out_idx: int = 0) -> None:
        self._record("setInput", idx)
        if src is None:
            old = self._inputs.pop(idx, None)
            if old is not None and self in old[0]._outputs:
                old[0]._outputs.remove(self)
            return
        self._inputs[idx] = (src, out_idx)
        if self not in src._outputs:
            src._outputs.append(self)

    def createNode(self, type_name: str, name: str) -> "_Node":
        self._record("createNode", type_name, name)
        path = self._path.rstrip("/") + "/" + name
        node = _Node(self._scene, self._spy, path, type_name, self._path)
        self._children.append(node)
        self._scene[path] = node
        return node

    def destroy(self) -> None:
        self._record("destroy")
        if self._fail_destroy:
            raise RuntimeError("destroy failed")
        self._scene.pop(self._path, None)
        for src, _out_idx in list(self._inputs.values()):
            if self in src._outputs:
                src._outputs.remove(self)


class _Undos:
    def __init__(self, spy: list) -> None:
        self._spy = spy

    def group(self, label: str):
        spy = self._spy

        @contextlib.contextmanager
        def _g():
            spy.append(("undo_group_begin", label))
            try:
                yield
            finally:
                spy.append(("undo_group_end", label))

        return _g()

    def disabler(self):
        spy = self._spy

        @contextlib.contextmanager
        def _d():
            spy.append(("undo_disable_begin",))
            try:
                yield
            finally:
                spy.append(("undo_disable_end",))

        return _d()


class _NodeFlag:
    DisplayComment = "DisplayComment"


class _HipFile:
    def name(self) -> str:
        return "/tmp/fake.hip"

    def addEventCallback(self, callback: object) -> None:
        pass

    def removeEventCallback(self, callback: object) -> None:
        pass


class _HipFileEventType:
    AfterClear = "AfterClear"
    AfterLoad = "AfterLoad"


class _FakeHou:
    def __init__(self, nodes: dict[str, _Node], spy: list) -> None:
        self._nodes, self._spy = nodes, spy
        self.undos = _Undos(spy)
        self.hipFile = _HipFile()
        self.hipFileEventType = _HipFileEventType()
        self.nodeFlag = _NodeFlag()

    def applicationVersionString(self) -> str:
        return "21.0.440"

    def selectedNodes(self) -> tuple:
        return ()

    def node(self, path: str):
        return self._nodes.get(path)


def _executor(spy: list) -> tuple[ChangeSetExecutor, dict[str, _Node]]:
    scene: dict[str, _Node] = {}
    scene["/obj"] = _Node(scene, spy, "/obj", "obj", "/", category="Obj")
    adapter = HoudiniSceneAdapter(_FakeHou(scene, spy))
    return ChangeSetExecutor(adapter), scene


def _built_container(scene: dict[str, _Node], spy: list) -> _Node:
    """A 'just promoted' container at /obj/table1 with box1 -> xform1 wired."""
    obj = scene["/obj"]
    container = obj.createNode("geo", "table1")
    box = container.createNode("box", "box1")
    xform = container.createNode("xform", "xform1")
    xform.setInput(0, box, 0)
    return container


# --------------------------------------------------------------------------
# _layered_layout (pure)
# --------------------------------------------------------------------------


def test_layout_chains_into_columns() -> None:
    positions = _layered_layout(
        ["/obj/t/a", "/obj/t/b", "/obj/t/c"],
        {
            "/obj/t/a": (),
            "/obj/t/b": ("/obj/t/a",),
            "/obj/t/c": ("/obj/t/b",),
        },
        anchor=(10.0, 20.0),
    )
    assert positions["/obj/t/a"] == (10.0, 20.0)
    assert positions["/obj/t/b"] == (13.0, 20.0)
    assert positions["/obj/t/c"] == (16.0, 20.0)


def test_layout_rows_within_a_column_follow_creation_order() -> None:
    positions = _layered_layout(
        ["/obj/t/a", "/obj/t/b", "/obj/t/out"],
        {
            "/obj/t/a": (),
            "/obj/t/b": (),
            "/obj/t/out": ("/obj/t/a", "/obj/t/b"),
        },
        anchor=(0.0, 0.0),
    )
    assert positions["/obj/t/a"] == (0.0, 0.0)
    assert positions["/obj/t/b"] == (0.0, -2.0)
    assert positions["/obj/t/out"] == (3.0, 0.0)


def test_layout_ignores_edges_outside_the_set_and_breaks_cycles() -> None:
    positions = _layered_layout(
        ["/obj/t/a", "/obj/t/b"],
        {
            "/obj/t/a": ("/obj/external/src", "/obj/t/b"),  # b: cycle member
            "/obj/t/b": ("/obj/t/a",),
        },
        anchor=(1.0, 1.0),
    )
    # Cycle is broken deterministically; every node still gets a position.
    assert set(positions) == {"/obj/t/a", "/obj/t/b"}


def test_layout_is_deterministic() -> None:
    edges = {"/obj/t/a": (), "/obj/t/b": ("/obj/t/a",)}
    first = _layered_layout(["/obj/t/a", "/obj/t/b"], edges, anchor=(5.0, 5.0))
    second = _layered_layout(["/obj/t/a", "/obj/t/b"], edges, anchor=(5.0, 5.0))
    assert first == second


# --------------------------------------------------------------------------
# _finalize_commit
# --------------------------------------------------------------------------


def test_finalize_lays_out_sets_flags_and_comments() -> None:
    spy: list = []
    executor, scene = _executor(spy)
    container = _built_container(scene, spy)
    box = scene["/obj/table1/box1"]
    xform = scene["/obj/table1/xform1"]
    warnings = executor._finalize_commit(
        container, xform, "/obj/table1", (("box1", "桌面粗模"),)
    )
    assert warnings == []
    # Layout: xform1 (depth 1) lands one column right of box1 (depth 0).
    assert box._position == (0.0, 0.0)
    assert xform._position == (3.0, 0.0)
    # Display + render flags on the resolved output node (SOP context).
    assert xform._display_flag is True
    assert xform._render_flag is True
    # Comment + display-comment flag on the annotated node.
    assert box._comment == "桌面粗模"
    assert box._generic_flags.get("DisplayComment") is True


def test_finalize_skips_render_flag_outside_sop_context() -> None:
    spy: list = []
    executor, scene = _executor(spy)
    container = _built_container(scene, spy)
    obj_level = scene["/obj"].createNode("geo", "asset")
    obj_level._category = "Obj"
    warnings = executor._finalize_commit(container, obj_level, "/obj/table1", ())
    assert warnings == []
    assert obj_level._display_flag is True
    assert obj_level._render_flag is False


def test_finalize_is_best_effort_when_children_unreadable() -> None:
    spy: list = []
    executor, scene = _executor(spy)
    container = _built_container(scene, spy)
    container._fail_children = True
    xform = scene["/obj/table1/xform1"]
    warnings = executor._finalize_commit(container, xform, "/obj/table1", ())
    assert len(warnings) == 1
    assert warnings[0].startswith("layout skipped:")
    # Flags still applied despite the layout failure.
    assert xform._display_flag is True


def test_finalize_warns_on_unknown_annotation_name() -> None:
    spy: list = []
    executor, scene = _executor(spy)
    container = _built_container(scene, spy)
    xform = scene["/obj/table1/xform1"]
    warnings = executor._finalize_commit(
        container, xform, "/obj/table1", (("ghost1", "不存在的节点"),)
    )
    assert any("ghost1" in w for w in warnings)
```

- [ ] **Step 4.2 — Run; expect import failure.** `uv run --frozen --extra eval pytest -q tests/runtime/test_scratch_finalize.py` → `ImportError: cannot import name '_layered_layout'`.

- [ ] **Step 4.3 — Implement the layout pure function and finalization in `houdini_side/changeset_executor.py`.**
  - Add `from collections.abc import Mapping, Sequence` to the imports (near line 41-47).
  - After `_UNDO_LABEL_PREFIX = "EEE Agent - "` (line 822) add:

```python
# Commit finalization layout grid: column per topology depth, row per node
# within the column. Fixed spacing keeps the layout deterministic.
_LAYOUT_COLUMN_WIDTH = 3.0
_LAYOUT_ROW_HEIGHT = 2.0


def _layered_layout(
    node_paths: Sequence[str],
    edges: Mapping[str, Sequence[str]],
    *,
    anchor: tuple[float, float],
) -> dict[str, tuple[float, float]]:
    """Deterministic topological layered layout for one committed node set.

    ``node_paths`` is the committed set in creation order (the stable
    tie-break); ``edges`` maps a path to its upstream (input) paths. A node
    with no in-set inputs sits at depth 0; every other node sits at
    ``1 + max(depth of its in-set inputs)``. Column = depth, row = creation
    order within the column; the block's top-left lands on ``anchor``. Edges
    pointing outside the set are ignored and cycles break at depth 0 (a SOP
    graph is a DAG, but the input is never trusted). Pure Python, no HOM.
    """
    members = set(node_paths)
    depths: dict[str, int] = {}
    visiting: set[str] = set()

    def depth(path: str) -> int:
        if path in depths:
            return depths[path]
        if path in visiting:
            return 0
        visiting.add(path)
        upstream = [u for u in edges.get(path, ()) if u in members]
        value = 0 if not upstream else 1 + max(depth(u) for u in upstream)
        visiting.discard(path)
        depths[path] = value
        return value

    columns: dict[int, list[str]] = {}
    for path in node_paths:
        columns.setdefault(depth(path), []).append(path)
    positions: dict[str, tuple[float, float]] = {}
    for column, paths in columns.items():
        for row, path in enumerate(paths):
            positions[path] = (
                anchor[0] + column * _LAYOUT_COLUMN_WIDTH,
                anchor[1] - row * _LAYOUT_ROW_HEIGHT,
            )
    return positions
```

  - In `scratch_commit`, inside the promotion undo group, after the `container.move(target_parent)` block (still inside `with hou.undos.group(...)`), append the finalization call. First declare `finalize_warnings: list[str] = []` next to `moved = False` (line ~2264), then inside the group after the move-if:

```python
                # Best-effort finalization (layout, display/render flags,
                # comments). Never raises: failures become warnings on the
                # commit result, so a cosmetic problem cannot fail a commit
                # that already passed the hard gates.
                finalize_warnings = self._finalize_commit(
                    container, output_node, final_path, request.annotations
                )
```

  Then in the committed `ScratchCommitResult(...)` return add `warnings=tuple(finalize_warnings),` (the refused return relies on the `warnings=()` default).

  - Add the method after `_scratch_output_node`:

```python
    def _finalize_commit(
        self,
        container: object,
        output_node: object,
        final_path: str,
        annotations: tuple[tuple[str, str], ...],
    ) -> list[str]:
        """Best-effort layout + display/render flags + comments post-promotion.

        Runs INSIDE the commit undo group so the user sees a single undo
        chunk. Each of the three steps is independent and never raises; a
        failure is collected as a bounded warning string returned to the
        Runtime on the commit result.
        """
        warnings: list[str] = []
        hou = self._hou

        # ---- 1. deterministic layered layout of the committed node set ----
        try:
            children = list(container.children())  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — best-effort
            children = []
            warnings.append(f"layout skipped: cannot list committed nodes: {exc}")
        if children:
            try:
                edges: dict[str, tuple[str, ...]] = {}
                xs: list[float] = []
                ys: list[float] = []
                for child in children:
                    upstream: list[str] = []
                    for conn in child.inputConnections() or []:
                        up = conn.outputNode()
                        if up is not None:
                            upstream.append(up.path())
                    edges[child.path()] = tuple(upstream)
                    pos = child.position()
                    xs.append(float(pos[0]))
                    ys.append(float(pos[1]))
                anchor = (min(xs), max(ys))
                positions = _layered_layout(
                    [child.path() for child in children], edges, anchor=anchor
                )
                for child in children:
                    target = positions.get(child.path())
                    if target is not None:
                        child.setPosition(target)
            except Exception as exc:  # noqa: BLE001 — best-effort
                warnings.append(f"layout failed: {exc}")

        # ---- 2. display (+ render for SOPs) flag on the output node -------
        try:
            output_node.setDisplayFlag(True)  # type: ignore[attr-defined]
            try:
                category = output_node.type().category().name()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — unknown category: skip render
                category = ""
            if category == "Sop":
                output_node.setRenderFlag(True)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — best-effort
            warnings.append(f"display flag failed: {exc}")

        # ---- 3. node comments from the Runtime-supplied annotations --------
        if annotations:
            try:
                by_name = {child.name(): child for child in children}
            except Exception:  # noqa: BLE001 — best-effort
                by_name = {}
            for name, comment in annotations:
                node = by_name.get(name)
                if node is None:
                    warnings.append(
                        f"comment skipped: no committed node named {name}"
                    )
                    continue
                try:
                    node.setComment(comment)
                    node.setGenericFlag(hou.nodeFlag.DisplayComment, True)  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001 — best-effort
                    warnings.append(f"comment failed on {name}: {exc}")

        return [w[:_MAX_ERROR_CHARS] for w in warnings[:_MAX_ERRORS]]
```

- [ ] **Step 4.4 — Run; expect pass.** `uv run --frozen --extra eval pytest -q tests/runtime/test_scratch_finalize.py -k "layout or finalize"` → all pass.

- [ ] **Step 4.5 — Commit.**
  `git add houdini_side/changeset_executor.py tests/runtime/test_scratch_finalize.py`
  `git commit -m "feat(executor): best-effort commit finalization (layout, display/render flags, comments)"`

---

## Task 5 — Executor `delete_nodes`, `scratch_topology`, and the `delete_node` scratch op

**Files:**
- Modify: `houdini_side/changeset_executor.py` (scratch import block lines 79-87; `scratch_exec` op loop lines 2078-2120; new methods after `scratch_destroy` line 2169-2202)
- Test: `tests/runtime/test_scratch_finalize.py` (append)

- [ ] **Step 5.1 — Append the failing tests** to `tests/runtime/test_scratch_finalize.py`:

```python
# --------------------------------------------------------------------------
# delete_nodes
# --------------------------------------------------------------------------


def _delete_request(paths, allowed, executor) -> ScratchDeleteRequest:
    return ScratchDeleteRequest.build(
        request_id="req_del",
        deadline_ms=5000,
        scene_epoch=executor.binding().scene_epoch,
        allowed_paths=tuple(allowed),
        paths=tuple(paths),
    )


def test_delete_nodes_happy_path_in_cleanup_undo_group() -> None:
    spy: list = []
    executor, scene = _executor(spy)
    container = _built_container(scene, spy)
    draft = container.createNode("box", "draft1")
    request = _delete_request(
        ["/obj/table1/draft1"],
        ["/obj/table1/draft1", "/obj/table1/box1"],
        executor,
    )
    result = executor.delete_nodes(request)
    assert result.deleted_paths == ("/obj/table1/draft1",)
    assert result.skipped == ()
    assert scene.get("/obj/table1/draft1") is None
    labels = [m[1] for m in spy if m[0] == "undo_group_begin"]
    assert "EEE Agent - cleanup" in labels


def test_delete_nodes_skips_externally_referenced_node() -> None:
    spy: list = []
    executor, scene = _executor(spy)
    container = _built_container(scene, spy)
    keeper = container.createNode("null", "keeper1")
    keeper.setInput(0, scene["/obj/table1/box1"], 0)
    request = _delete_request(
        ["/obj/table1/box1"],
        ["/obj/table1/box1"],
        executor,
    )
    result = executor.delete_nodes(request)
    assert result.deleted_paths == ()
    assert result.skipped[0]["path"] == "/obj/table1/box1"
    assert "still referenced by /obj/table1/keeper1" in result.skipped[0]["reason"]
    assert scene.get("/obj/table1/box1") is not None


def test_delete_nodes_deletes_referenced_node_when_consumer_also_deleted() -> None:
    spy: list = []
    executor, scene = _executor(spy)
    container = _built_container(scene, spy)
    draft = container.createNode("null", "draft1")
    draft.setInput(0, scene["/obj/table1/box1"], 0)
    paths = ["/obj/table1/box1", "/obj/table1/draft1"]
    request = _delete_request(paths, paths, executor)
    result = executor.delete_nodes(request)
    assert set(result.deleted_paths) == set(paths)
    assert result.skipped == ()


def test_delete_nodes_skips_missing_and_failing_nodes() -> None:
    spy: list = []
    executor, scene = _executor(spy)
    container = _built_container(scene, spy)
    broken = container.createNode("box", "broken1")
    broken._fail_destroy = True
    paths = ["/obj/table1/broken1", "/obj/table1/ghost1"]
    request = _delete_request(paths, paths, executor)
    result = executor.delete_nodes(request)
    assert result.deleted_paths == ()
    reasons = {s["path"]: s["reason"] for s in result.skipped}
    assert reasons["/obj/table1/ghost1"] == "node does not exist"
    assert reasons["/obj/table1/broken1"].startswith("destroy failed:")


# --------------------------------------------------------------------------
# scratch_topology
# --------------------------------------------------------------------------


def test_scratch_topology_reports_wiring_and_missing_nodes() -> None:
    spy: list = []
    executor, scene = _executor(spy)
    _built_container(scene, spy)
    scene["/obj/table1/xform1"].setDisplayFlag(True)
    request = ScratchTopologyRequest.build(
        request_id="req_topo",
        deadline_ms=5000,
        scene_epoch=executor.binding().scene_epoch,
        paths=("/obj/table1/box1", "/obj/table1/xform1", "/obj/table1/ghost1"),
    )
    result = executor.scratch_topology(request)
    by_path = {n["path"]: n for n in result.nodes}
    assert by_path["/obj/table1/box1"]["outputs"] == ["/obj/table1/xform1"]
    assert by_path["/obj/table1/box1"]["inputs"] == []
    assert by_path["/obj/table1/xform1"]["inputs"] == ["/obj/table1/box1"]
    assert by_path["/obj/table1/xform1"]["display_flag"] is True
    assert by_path["/obj/table1/ghost1"]["exists"] is False


# --------------------------------------------------------------------------
# delete_node scratch op
# --------------------------------------------------------------------------


def test_scratch_exec_delete_node_op_removes_sandbox_node() -> None:
    spy: list = []
    executor, scene = _executor(spy)
    epoch = executor.binding().scene_epoch
    build = ScratchRequest.build(
        request_id="req_b1",
        deadline_ms=5000,
        scene_epoch=epoch,
        sandbox_id="run1",
        operations=(
            ScratchOp(kind="create_node", node_name="box1", node_type="box"),
            ScratchOp(kind="create_node", node_name="draft1", node_type="box"),
        ),
        purpose="build with a draft node",
    )
    executor.scratch_exec(build)
    assert scene.get("/obj/eee_scratch_run1/draft1") is not None
    delete = ScratchRequest.build(
        request_id="req_b2",
        deadline_ms=5000,
        scene_epoch=epoch,
        sandbox_id="run1",
        operations=(ScratchOp(kind="delete_node", node_name="draft1"),),
        purpose="remove the draft node",
    )
    result = executor.scratch_exec(delete)
    assert result.applied_ops == 1
    assert scene.get("/obj/eee_scratch_run1/draft1") is None
    assert scene.get("/obj/eee_scratch_run1/box1") is not None
```

- [ ] **Step 5.2 — Run; expect failures.** `uv run --frozen --extra eval pytest -q tests/runtime/test_scratch_finalize.py -k "delete or topology"` → fails (`AttributeError: 'ChangeSetExecutor' object has no attribute 'delete_nodes'`, and `delete_node` op no-ops).

- [ ] **Step 5.3 — Implement in `houdini_side/changeset_executor.py`.**
  - Extend the scratch import block (lines 79-87) with `ScratchDeleteRequest`, `ScratchDeleteResult`, `ScratchTopologyRequest`, `ScratchTopologyResult`.
  - In `scratch_exec`, add a branch after the `connect` branch (inside the op loop):

```python
                    elif op.kind == "delete_node":
                        node = node_index.get(op.node_name)
                        if node is None:
                            node = hou.node(
                                f"{container_path}/{op.node_name}"
                            )
                        if node is None:
                            raise _scratch_failed(
                                f"delete_node target node not found: {op.node_name}"
                            )
                        node.destroy()
                        node_index.pop(op.node_name, None)
                        applied += 1
```

  - Add after `scratch_destroy`:

```python
    def delete_nodes(self, request: ScratchDeleteRequest) -> ScratchDeleteResult:
        """Delete committed nodes from a Runtime-supplied allowlist.

        Fail-closed per path: a node is deleted only when its path is in the
        allowlist, it exists, and no node OUTSIDE the delete set consumes it.
        The whole batch runs in one ``"EEE Agent - cleanup"`` undo group so the
        user can undo the cleanup as a single chunk. Skips are reported with
        reasons — never silent.
        """
        binding = self.binding()
        if binding.scene_epoch != request.scene_epoch:
            raise _stale(
                "The scene changed before the delete operation could run."
            )
        hou = self._hou
        allowed = set(request.allowed_paths)
        delete_set = set(request.paths)
        deleted: list[str] = []
        skipped: list[dict[str, object]] = []
        with hou.undos.group(_UNDO_LABEL_PREFIX + "cleanup"):
            for path in request.paths:
                if path not in allowed:
                    skipped.append({"path": path, "reason": "not in the allowlist"})
                    continue
                node = hou.node(path)
                if node is None:
                    skipped.append({"path": path, "reason": "node does not exist"})
                    continue
                try:
                    consumers = [o.path() for o in (node.outputs() or [])]
                except Exception as exc:  # noqa: BLE001 — fail closed
                    skipped.append({
                        "path": path,
                        "reason": f"topology read failed: {exc}"[:_MAX_ERROR_CHARS],
                    })
                    continue
                external = [c for c in consumers if c not in delete_set]
                if external:
                    skipped.append({
                        "path": path,
                        "reason": f"still referenced by {external[0]}",
                    })
                    continue
                try:
                    node.destroy()
                    deleted.append(path)
                except Exception as exc:  # noqa: BLE001 — fail closed
                    skipped.append({
                        "path": path,
                        "reason": f"destroy failed: {exc}"[:_MAX_ERROR_CHARS],
                    })
        return ScratchDeleteResult(
            deleted_paths=tuple(deleted),
            skipped=tuple(skipped),
        )

    def scratch_topology(self, request: ScratchTopologyRequest) -> ScratchTopologyResult:
        """Read-only per-path wiring facts for cleanup decisions.

        Never mutates the scene; runs on the same main-thread FIFO as every
        other bridge operation. A missing node is reported as
        ``exists=False`` — a normal outcome during cleanup analysis, not an
        error.
        """
        binding = self.binding()
        if binding.scene_epoch != request.scene_epoch:
            raise _stale(
                "The scene changed before the topology query could run."
            )
        hou = self._hou
        nodes: list[dict[str, object]] = []
        for path in request.paths:
            node = hou.node(path)
            if node is None:
                nodes.append({
                    "path": path,
                    "exists": False,
                    "inputs": [],
                    "outputs": [],
                    "display_flag": False,
                })
                continue
            inputs = [
                conn.outputNode().path()
                for conn in (node.inputConnections() or [])
            ][:32]
            outputs = [o.path() for o in (node.outputs() or [])][:32]
            nodes.append({
                "path": path,
                "exists": True,
                "inputs": inputs,
                "outputs": outputs,
                "display_flag": bool(node.isDisplayFlagSet()),
            })
        return ScratchTopologyResult(nodes=tuple(nodes))
```

- [ ] **Step 5.4 — Run; expect pass.** `uv run --frozen --extra eval pytest -q tests/runtime/test_scratch_finalize.py` → all pass.

- [ ] **Step 5.5 — Commit.**
  `git add houdini_side/changeset_executor.py tests/runtime/test_scratch_finalize.py`
  `git commit -m "feat(executor): delete_nodes allowlist delete + scratch_topology + delete_node op"`

---

## Task 6 — ScratchCoordinator: store recording, commit annotations, graceful degradation + service wiring

**Files:**
- Modify: `eee_agent/modeling/scratch_coordinator.py` (imports lines 23-46; `ScratchSessionContext` lines 93-111; `ScratchCoordinator` init/build/commit lines 123-197)
- Modify: `eee_agent/runtime/agent_context.py` (`RuntimeToolContext` lines 54-67)
- Modify: `eee_agent/runtime/service.py` (`__init__` repository block lines 500-510; `_build_scratch_context` lines 1898-1925; `_build_runtime_context` lines 1873-1897)
- Test: `tests/runtime/test_scratch_bridge.py` (modify — coordinator tests)

- [ ] **Step 6.1 — Write the failing tests.** In `tests/runtime/test_scratch_bridge.py`, update the `_coordinator` helper to accept a store and run id, seed a run row for store-backed tests, and append:

```python
# --- task graph recording (Task 6) -----------------------------------------

from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.task_graph import TaskGraphStore


async def _seed_run(db: RuntimeDatabase, run_id: str = "run_1") -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with db.write_transaction() as conn:
        await conn.execute(
            "INSERT INTO sessions(session_id, title, status, created_at, "
            "updated_at, last_seq, replay_floor_seq) "
            "VALUES ('sess_1', 't', 'active', ?, ?, 0, 0)",
            (now, now),
        )
        await conn.execute(
            "INSERT INTO runs(run_id, session_id, status, user_input, "
            "created_at, model_snapshot_json) "
            "VALUES (?, 'sess_1', 'Planning', 'build a table', ?, '{}')",
            (run_id, now),
        )


def _coordinator(provider, *, store=None, run_id: str = "run_1"):
    session_ctx = ScratchSessionContext(
        provider=provider, sandbox_id="run1", run_id=run_id, task_store=store
    )
    return ScratchCoordinator(session_ctx)


def test_build_records_step_and_nodes(tmp_path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(tmp_path / "app.sqlite")
        try:
            await _seed_run(db)
            store = TaskGraphStore(db)
            provider = _FakeScratchProvider()
            coord = _coordinator(provider, store=store)
            result = await coord.build(
                purpose="创建桌腿",
                operations=[
                    {"kind": "create_node", "node_name": "leg1", "node_type": "tube",
                     "note": "第一条腿"},
                    {"kind": "set_parm", "node_name": "leg1", "parm": "rad1", "value": 0.5},
                ],
            )
            assert result["ok"] is True
            steps = await store.list_run_steps("run_1")
            assert len(steps) == 1
            assert steps[0].purpose == "创建桌腿"
            assert steps[0].tool == "scratch_build"
            assert steps[0].status == "open"
            assert [n.node_path for n in steps[0].nodes] == [
                "/obj/eee_scratch_run1/leg1"
            ]
            assert steps[0].nodes[0].note == "第一条腿"
            assert steps[0].nodes[0].node_type == "tube"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_commit_sends_annotations_and_marks_committed(tmp_path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(tmp_path / "app.sqlite")
        try:
            await _seed_run(db)
            store = TaskGraphStore(db)
            provider = _FakeScratchProvider()
            coord = _coordinator(provider, store=store)
            await coord.build(
                purpose="桌面建模",
                operations=[
                    {"kind": "create_node", "node_name": "box1", "node_type": "box",
                     "note": "桌面粗模"},
                    {"kind": "create_node", "node_name": "blast1", "node_type": "blast"},
                ],
            )
            result = await coord.commit(
                target_parent_path="/obj", target_name="table1"
            )
            assert result["committed"] is True
            # Annotations: node note wins; missing note falls back to purpose.
            commit_call = provider.commit_calls[0]
            assert dict(commit_call["annotations"]) == {
                "blast1": "桌面建模",
                "box1": "桌面粗模",
            }
            steps = await store.list_run_steps("run_1")
            assert steps[0].status == "committed"
            node_by_name = {
                n.node_path.rsplit("/", 1)[-1]: n for n in steps[0].nodes
            }
            assert node_by_name["box1"].status == "committed"
            assert node_by_name["box1"].committed_path == "/obj/table1/box1"
            assert result["warnings"] == []
        finally:
            await db.close()

    asyncio.run(scenario())


def test_store_failure_degrades_without_blocking_build(tmp_path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(tmp_path / "app.sqlite")
        try:
            # NO run row: the FK constraint makes every store write fail.
            store = TaskGraphStore(db)
            provider = _FakeScratchProvider()
            coord = _coordinator(provider, store=store)
            first = await coord.build(
                purpose="桌腿",
                operations=[
                    {"kind": "create_node", "node_name": "leg1", "node_type": "tube"},
                ],
            )
            assert first["ok"] is True  # modeling never blocked by the graph
            assert coord._store_failed is True
            second = await coord.build(
                purpose="桌面",
                operations=[
                    {"kind": "create_node", "node_name": "top1", "node_type": "box"},
                ],
            )
            assert second["ok"] is True
            assert (await store.list_run_steps("run_1")) == ()
        finally:
            await db.close()

    asyncio.run(scenario())
```

  Update `_FakeScratchProvider` to record commit calls: add `self.commit_calls: list[dict[str, Any]] = []` in `__init__` and `self.commit_calls.append({"sandbox_id": sandbox_id, "annotations": annotations, ...})` at the top of `scratch_commit` (which now receives `annotations: tuple = ()`). Also add `commit_calls` coverage everywhere the fake is constructed. All pre-existing `_coordinator(provider)` call sites keep working via the new default (`store=None, run_id="run_1"`). The new test code needs `from datetime import datetime, timezone` at the top of the file if not already imported.

- [ ] **Step 6.2 — Run; expect failures.** `uv run --frozen --extra eval pytest -q tests/runtime/test_scratch_bridge.py -k "records or annotations or degrades"` → fails (`TypeError: ScratchSessionContext.__init__() got an unexpected keyword argument 'run_id'`).

- [ ] **Step 6.3 — Implement coordinator + context changes.**
  - `eee_agent/modeling/scratch_coordinator.py`:
    - Add `import logging` and `from datetime import ...` is NOT needed; add `from eee_agent.runtime.task_graph import TaskGraphStore`; module-level `_log = logging.getLogger(__name__)`.
    - `ScratchSessionContext`: add fields and validation:

      ```python
      @dataclass(frozen=True, slots=True)
      class ScratchSessionContext:
          """Per-run trusted scratch context consumed by :func:`scratch_build`.

          Carries the injected provider, the run-scoped sandbox id prefix, the
          run id, and an optional task graph store handle. When the store is
          None (or a write fails once), the coordinator still builds/commits —
          the task graph degrades to "unavailable for this run".
          """

          provider: ScratchProvider
          sandbox_id: str
          run_id: str
          task_store: TaskGraphStore | None = None

          def __post_init__(self) -> None:
              if self.provider is None or not isinstance(self.provider, ScratchProvider):
                  raise TypeError(
                      "ScratchSessionContext.provider must implement ScratchProvider"
                  )
              if type(self.sandbox_id) is not str or not self.sandbox_id:
                  raise TypeError("ScratchSessionContext.sandbox_id must be a non-empty string")
              if type(self.run_id) is not str or not self.run_id:
                  raise TypeError("ScratchSessionContext.run_id must be a non-empty string")
              if self.task_store is not None and type(self.task_store) is not TaskGraphStore:
                  raise TypeError("ScratchSessionContext.task_store must be a TaskGraphStore")
      ```

    - `ScratchCoordinator.__init__` adds `self._store_failed = False`. Update the class docstring: replace "Owns no state." with "Records steps/nodes into the injected task graph store on success; a store failure flips `_store_failed` and the graph silently degrades for the rest of the run — modeling is never blocked by recording."
    - `ScratchCoordinator.build`: after the provider call succeeds, before `return self._summarize(result)`, add `await self._record_build(purpose, typed_ops, result)`.
    - `ScratchCoordinator.commit`: compute `annotations = await self._commit_annotations()` before the provider call and pass `annotations=annotations`; after the call, add:

      ```python
              if result.committed:
                  await self._record_commit(result)
              return self._summarize_commit(result)
      ```

    - New private methods:

      ```python
          async def _record_build(
              self,
              purpose: str,
              typed_ops: tuple[ScratchOp, ...],
              result: ScratchResult,
          ) -> None:
              """Record the build step + created nodes. Never raises."""
              store = self._context.task_store
              if store is None or self._store_failed:
                  return
              try:
                  step = await store.record_step(
                      run_id=self._context.run_id,
                      tool="scratch_build",
                      purpose=purpose,
                  )
                  created = tuple(
                      (
                          f"{result.sandbox_root}/{op.node_name}",
                          op.node_type,
                          op.note or None,
                      )
                      for op in typed_ops
                      if op.kind == "create_node"
                  )
                  if created:
                      await store.record_nodes(step_id=step.step_id, nodes=created)
              except Exception:  # noqa: BLE001 — recording must never block modeling
                  self._store_failed = True
                  _log.exception(
                      "task graph recording failed; degrading for run=%s",
                      self._context.run_id,
                  )

          async def _commit_annotations(self) -> tuple[tuple[str, str], ...]:
              """Per-node commit comments: node note, else the step purpose."""
              store = self._context.task_store
              if store is None or self._store_failed:
                  return ()
              try:
                  steps = await store.list_run_steps(self._context.run_id)
              except Exception:  # noqa: BLE001 — annotations are best-effort
                  self._store_failed = True
                  _log.exception(
                      "task graph read failed; committing without annotations (run=%s)",
                      self._context.run_id,
                  )
                  return ()
              annotations: dict[str, str] = {}
              for step in steps:
                  for node in step.nodes:
                      if node.status != "sandbox":
                          continue
                      name = node.node_path.rsplit("/", 1)[-1]
                      annotations[name] = (node.note or step.purpose)[:500]
              return tuple(sorted(annotations.items()))

          async def _record_commit(self, result: ScratchCommitResult) -> None:
              """Mark the run's sandbox nodes committed. Never raises."""
              store = self._context.task_store
              if store is None or self._store_failed:
                  return
              try:
                  await store.mark_committed(
                      run_id=self._context.run_id,
                      sandbox_root=f"/obj/eee_scratch_{self._context.sandbox_id}",
                      final_path=result.final_path,
                  )
              except Exception:  # noqa: BLE001 — recording must never block modeling
                  self._store_failed = True
                  _log.exception(
                      "task graph commit recording failed; degrading for run=%s",
                      self._context.run_id,
                  )
      ```

  - `eee_agent/runtime/agent_context.py` — add the field to `RuntimeToolContext`:

    ```python
        read_only: ReadOnlyProvider
        knowledge: KnowledgeProvider
        modeling: object | None = None
        scratch: object | None = None
        task_graph: object | None = None
    ```

  - `eee_agent/runtime/service.py`:
    - In `__init__` next to the other repository constructions (after `self._events = EventStore(database)`, line ~508):

      ```python
              from eee_agent.runtime.task_graph import TaskGraphStore

              self._task_store = TaskGraphStore(database)
      ```

    - In `_build_scratch_context`, change the `ScratchSessionContext(...)` call to:

      ```python
              session_ctx = ScratchSessionContext(
                  provider=provider,
                  sandbox_id=sandbox_id,
                  run_id=run_id,
                  task_store=getattr(self, "_task_store", None),
              )
      ```

    - In `_build_runtime_context`, before the `return RuntimeToolContext(...)`:

      ```python
              task_graph = None
              task_store = getattr(self, "_task_store", None)
              if task_store is not None:
                  from eee_agent.runtime.task_graph import TaskGraphToolContext

                  task_graph = TaskGraphToolContext(store=task_store, run_id=run_id)
      ```

      and add `task_graph=task_graph` to the `RuntimeToolContext(...)` call.

- [ ] **Step 6.4 — Run; expect pass.** `uv run --frozen --extra eval pytest -q tests/runtime/test_scratch_bridge.py tests/runtime/test_task_graph.py` → all pass. Then the runtime slice: `uv run --frozen --extra eval pytest -q tests/runtime tests/panel` → all pass (this catches `RuntimeToolContext` construction sites and service wiring tests).

- [ ] **Step 6.5 — Commit.**
  `git add eee_agent/modeling/scratch_coordinator.py eee_agent/runtime/agent_context.py eee_agent/runtime/service.py tests/runtime/test_scratch_bridge.py`
  `git commit -m "feat(modeling): record build/commit steps into the task graph with graceful degradation"`

---

## Task 7 — LLM context injection of the task summary

**Files:**
- Create: `eee_agent/task_summary.py`
- Modify: `eee_agent/app.py` (middleware assembly, after the context_trim block lines 67-73)
- Test: `tests/runtime/test_task_summary.py` (create)

The injection seam is a LangChain `AgentMiddleware.awrap_model_call` — the same pattern as `eee_agent/loop_guard.py:149-162` and `eee_agent/context_trim.py:82-100` (verified: `agent_runner.py` does NOT assemble messages; per-call dynamic state goes through middleware, and `ModelRequest.runtime.context` carries the per-run `RuntimeToolContext`).

- [ ] **Step 7.1 — Write the failing tests.** Create `tests/runtime/test_task_summary.py`:

```python
"""Task summary injection middleware tests.

Drives ``TaskSummaryMiddleware._inject``/``awrap_model_call`` with a minimal
fake ModelRequest and a real TaskGraphStore on a tmp database. No LLM, no
Houdini.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import SystemMessage

from eee_agent.runtime.agent_context import RuntimeToolContext
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.task_graph import TaskGraphStore, TaskGraphToolContext
from eee_agent.task_summary import TaskSummaryMiddleware, is_enabled


def _run(coro):
    return asyncio.run(coro)


class _FakeReadOnly:
    async def scene_status(self):
        return {}

    async def query_scene(self, node_paths):
        return {}

    async def inspect_workspace(self, workspace_id):
        return {}

    async def geometry_stats(self, node_path):
        return {}

    async def work_status(self, workspace_id):
        return {}


class _FakeKnowledge:
    def search(self, query, *, limit=5):
        return {}

    def get(self, entity_id, *, max_body_bytes=8000):
        return {}


class _FakeRuntime:
    def __init__(self, context: object) -> None:
        self.context = context


class _FakeRequest:
    """Minimal stand-in for langchain's ModelRequest (runtime + system msg)."""

    def __init__(self, context: object, system_message=None) -> None:
        self.runtime = _FakeRuntime(context)
        self.system_message = system_message

    def override(self, *, system_message):
        return _FakeRequest(self.runtime.context, system_message)


def _context(store: TaskGraphStore | None, run_id: str = "run_1") -> RuntimeToolContext:
    return RuntimeToolContext(
        read_only=_FakeReadOnly(),
        knowledge=_FakeKnowledge(),
        task_graph=(
            TaskGraphToolContext(store=store, run_id=run_id)
            if store is not None
            else None
        ),
    )


async def _open_with_step(db_path: Path) -> tuple[RuntimeDatabase, TaskGraphStore]:
    db = await RuntimeDatabase.open(db_path)
    now = datetime.now(timezone.utc).isoformat()
    async with db.write_transaction() as conn:
        await conn.execute(
            "INSERT INTO sessions(session_id, title, status, created_at, "
            "updated_at, last_seq, replay_floor_seq) "
            "VALUES ('sess_1', 't', 'active', ?, ?, 0, 0)",
            (now, now),
        )
        await conn.execute(
            "INSERT INTO runs(run_id, session_id, status, user_input, "
            "created_at, model_snapshot_json) "
            "VALUES ('run_1', 'sess_1', 'Planning', 'build', ?, '{}')",
            (now,),
        )
    store = TaskGraphStore(db)
    step = await store.record_step(
        run_id="run_1", tool="scratch_build", purpose="桌面建模"
    )
    await store.record_nodes(
        step_id=step.step_id,
        nodes=[("/obj/eee_scratch_run_1/box1", "box", None)],
    )
    return db, store


def test_is_enabled_default_on() -> None:
    assert is_enabled() is True


def test_inject_appends_summary_to_system_message(tmp_path: Path) -> None:
    async def scenario() -> None:
        db, store = await _open_with_step(tmp_path / "app.sqlite")
        try:
            request = _FakeRequest(
                _context(store), SystemMessage(content="BASE PROMPT")
            )
            middleware = TaskSummaryMiddleware()
            injected = await middleware._inject(request)
            content = injected.system_message.content
            assert content.startswith("BASE PROMPT")
            assert "Task state (run run_1):" in content
            assert "1. [open] 桌面建模 (box1)" in content
        finally:
            await db.close()

    _run(scenario())


def test_inject_no_steps_leaves_request_unchanged(tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(tmp_path / "app.sqlite")
        try:
            store = TaskGraphStore(db)
            request = _FakeRequest(_context(store), SystemMessage(content="BASE"))
            injected = await TaskSummaryMiddleware()._inject(request)
            assert injected is request
        finally:
            await db.close()

    _run(scenario())


def test_inject_without_task_graph_context_is_passthrough() -> None:
    request = _FakeRequest(_context(None), SystemMessage(content="BASE"))
    injected = _run(TaskSummaryMiddleware()._inject(request))
    assert injected is request
    # A foreign context type must also pass through.
    other = _FakeRequest(object(), SystemMessage(content="BASE"))
    assert _run(TaskSummaryMiddleware()._inject(other)) is other


def test_inject_swallows_store_failures(tmp_path: Path) -> None:
    async def scenario() -> None:
        db, store = await _open_with_step(tmp_path / "app.sqlite")
        await db.close()  # every store call now raises
        request = _FakeRequest(_context(store), SystemMessage(content="BASE"))
        injected = await TaskSummaryMiddleware()._inject(request)
        assert injected is request

    _run(scenario())


def test_awrap_model_call_injects(tmp_path: Path) -> None:
    async def scenario() -> None:
        db, store = await _open_with_step(tmp_path / "app.sqlite")
        try:
            request = _FakeRequest(_context(store), SystemMessage(content="BASE"))
            seen = {}

            async def handler(req):
                seen["content"] = req.system_message.content
                return "response"

            result = await TaskSummaryMiddleware().awrap_model_call(request, handler)
            assert result == "response"
            assert "Task state (run run_1):" in seen["content"]
        finally:
            await db.close()

    _run(scenario())
```

- [ ] **Step 7.2 — Run; expect import failure.** `uv run --frozen --extra eval pytest -q tests/runtime/test_task_summary.py` → `ModuleNotFoundError: No module named 'eee_agent.task_summary'`.

- [ ] **Step 7.3 — Create `eee_agent/task_summary.py`:**

```python
"""Per-LLM-call task-state injection.

Appends the compact task-graph summary (what the agent has built so far this
run, and why) to the system message before every model call, so the agent
always sees its current task state instead of re-deriving it from tool
history. The summary comes from the per-run
:class:`~eee_agent.runtime.task_graph.TaskGraphStore` injected through
``RuntimeToolContext.task_graph``; when there is no store (non-modeling runs)
or no recorded steps, the request passes through unchanged. Every failure is
swallowed — context injection must never break a model call.

Opt out with EEE_TASK_SUMMARY=false.
"""

from __future__ import annotations

import os
from typing import Any, Callable, List

from typing_extensions import override

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import SystemMessage

from eee_agent.runtime.agent_context import RuntimeToolContext
from eee_agent.runtime.task_graph import TaskGraphToolContext


def is_enabled() -> bool:
    """Default ON; set EEE_TASK_SUMMARY=false to disable."""
    return os.getenv("EEE_TASK_SUMMARY", "true").strip().lower() != "false"


def _append_system(
    request: ModelRequest[ContextT], text: str
) -> ModelRequest[ContextT]:
    sm = request.system_message
    if sm is None:
        return request.override(system_message=SystemMessage(content=text))
    content = sm.content
    if isinstance(content, str):
        return request.override(
            system_message=SystemMessage(content=content + "\n\n" + text)
        )
    return request.override(
        system_message=SystemMessage(
            content=[*content, {"type": "text", "text": text}]
        )
    )


class TaskSummaryMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Append the current run's task summary to the system message per call."""

    @staticmethod
    def _task_graph(request: ModelRequest[ContextT]) -> TaskGraphToolContext | None:
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        if type(context) is not RuntimeToolContext:
            return None
        task_graph = getattr(context, "task_graph", None)
        return task_graph if type(task_graph) is TaskGraphToolContext else None

    async def _inject(
        self, request: ModelRequest[ContextT]
    ) -> ModelRequest[ContextT]:
        task_graph = self._task_graph(request)
        if task_graph is None:
            return request
        try:
            summary = await task_graph.store.render_summary(task_graph.run_id)
        except Exception:  # noqa: BLE001 — injection must never break a call
            return request
        if not summary:
            return request
        return _append_system(request, summary)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        # The sync path cannot await the store; the Runtime graph always runs
        # async, so the sync wrapper is a pass-through.
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        return await handler(await self._inject(request))
```

- [ ] **Step 7.4 — Register the middleware in `eee_agent/app.py`.** Insert immediately after the `context_trim` try/except block (after line 73), mirroring the surrounding style:

```python
    try:
        from eee_agent import task_summary
        if task_summary.is_enabled():
            middleware.append(task_summary.TaskSummaryMiddleware())
            _log.info("task summary injection middleware enabled")
    except Exception as e:  # noqa: BLE001
        _log.warning("task summary injection disabled: %s", e, exc_info=True)
```

- [ ] **Step 7.5 — Run; expect pass.** `uv run --frozen --extra eval pytest -q tests/runtime/test_task_summary.py tests/test_harness.py` → all pass (the harness test guards the middleware/graph assembly contract).

- [ ] **Step 7.6 — Commit.**
  `git add eee_agent/task_summary.py eee_agent/app.py tests/runtime/test_task_summary.py`
  `git commit -m "feat(agent): inject per-run task graph summary into every LLM call"`

---

## Task 8 — `cleanup_nodes` tool (two-phase)

**Files:**
- Modify: `eee_agent/modeling/scratch_coordinator.py` (provider Protocol lines 57-91; new `cleanup`/`_cleanup_suggest`/`_cleanup_execute` methods; new `cleanup_nodes` tool; imports)
- Modify: `eee_agent/runtime/agent_runner.py` (modeling tool registration lines 348-356)
- Test: `tests/runtime/test_scratch_bridge.py` (modify — fake provider + cleanup tests)

- [ ] **Step 8.1 — Write the failing tests.** In `tests/runtime/test_scratch_bridge.py`, extend the fake provider and append cleanup tests:

```python
from types import SimpleNamespace

from eee_agent.houdini_bridge.scratch import (
    ScratchDeleteResult,
    ScratchTopologyResult,
)


class _FakeCleanupProvider(_FakeScratchProvider):
    """Adds scripted scene_topology / delete_nodes responses."""

    def __init__(self, *, topology=(), delete_result=None, **kwargs):
        super().__init__(**kwargs)
        self._topology = topology
        self._delete_result = delete_result
        self.topology_calls: list[tuple[str, ...]] = []
        self.delete_calls: list[dict[str, Any]] = []

    async def scene_topology(self, *, paths):
        self.topology_calls.append(paths)
        return ScratchTopologyResult(nodes=tuple(self._topology))

    async def delete_nodes(self, *, paths, allowed_paths):
        self.delete_calls.append({"paths": paths, "allowed_paths": allowed_paths})
        return self._delete_result


def _topo(path, outputs=(), exists=True):
    return {
        "path": path,
        "exists": exists,
        "inputs": [],
        "outputs": list(outputs),
        "display_flag": False,
    }


async def _seed_graph(store: TaskGraphStore) -> None:
    """One committed orphan node + one still-sandbox draft node."""
    step = await store.record_step(
        run_id="run_1", tool="scratch_build", purpose="废弃件"
    )
    await store.record_nodes(
        step_id=step.step_id,
        nodes=[("/obj/eee_scratch_run1/orphan1", "tube", None)],
    )
    await store.mark_committed(
        run_id="run_1",
        sandbox_root="/obj/eee_scratch_run1",
        final_path="/obj/table1",
    )
    # Recorded AFTER the commit, so it stays in sandbox state.
    step2 = await store.record_step(
        run_id="run_1", tool="scratch_build", purpose="草稿"
    )
    await store.record_nodes(
        step_id=step2.step_id,
        nodes=[("/obj/eee_scratch_run1/draft1", "box", None)],
    )


def test_cleanup_suggest_finds_leaf_candidates(tmp_path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(tmp_path / "app.sqlite")
        try:
            await _seed_run(db)
            store = TaskGraphStore(db)
            await _seed_graph(store)
            provider = _FakeCleanupProvider(
                topology=(
                    _topo("/obj/eee_scratch_run1/draft1"),
                    _topo("/obj/table1/orphan1", outputs=["/obj/table1/out1"]),
                ),
            )
            coord = _coordinator(provider, store=store)
            result = await coord.cleanup()
            assert result["ok"] is True
            assert result["phase"] == "suggest"
            assert result["candidates"] == [
                {
                    "path": "/obj/eee_scratch_run1/draft1",
                    "reason": "nothing is wired downstream of it",
                }
            ]
            # Suggest never deletes.
            assert provider.calls == []
            assert provider.delete_calls == []
        finally:
            await db.close()

    asyncio.run(scenario())


def test_cleanup_suggest_requires_store() -> None:
    provider = _FakeCleanupProvider()
    coord = _coordinator(provider, store=None)
    result = asyncio.run(coord.cleanup())
    assert result["ok"] is False
    assert result["code"] == "cleanup.store_unavailable"


def test_cleanup_execute_deletes_and_marks_store(tmp_path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(tmp_path / "app.sqlite")
        try:
            await _seed_run(db)
            store = TaskGraphStore(db)
            await _seed_graph(store)
            provider = _FakeCleanupProvider(
                topology=(
                    _topo("/obj/eee_scratch_run1/draft1"),
                    _topo("/obj/table1/orphan1"),
                ),
                delete_result=ScratchDeleteResult(
                    deleted_paths=("/obj/table1/orphan1",), skipped=()
                ),
            )
            coord = _coordinator(provider, store=store)
            result = await coord.cleanup(
                paths=["/obj/eee_scratch_run1/draft1", "/obj/table1/orphan1"]
            )
            assert result["ok"] is True
            assert result["phase"] == "execute"
            assert set(result["deleted"]) == {
                "/obj/eee_scratch_run1/draft1",
                "/obj/table1/orphan1",
            }
            assert result["skipped"] == []
            # Sandbox node went through the delete_node scratch op.
            assert provider.calls[0]["operations"][0].kind == "delete_node"
            assert provider.calls[0]["operations"][0].node_name == "draft1"
            # Committed node went through the allowlisted delete.
            assert provider.delete_calls[0]["paths"] == ("/obj/table1/orphan1",)
            assert provider.delete_calls[0]["allowed_paths"] == ("/obj/table1/orphan1",)
            # The graph reflects the deletion.
            steps = await store.list_run_steps("run_1")
            assert all(s.status == "deleted" for s in steps)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_cleanup_execute_rejects_foreign_and_referenced(tmp_path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(tmp_path / "app.sqlite")
        try:
            await _seed_run(db)
            store = TaskGraphStore(db)
            await _seed_graph(store)
            provider = _FakeCleanupProvider(
                topology=(
                    _topo("/obj/eee_scratch_run1/draft1",
                          outputs=["/obj/eee_scratch_run1/keeper1"]),
                ),
                delete_result=ScratchDeleteResult(deleted_paths=(), skipped=()),
            )
            coord = _coordinator(provider, store=store)
            result = await coord.cleanup(
                paths=["/obj/other/scene_node", "/obj/eee_scratch_run1/draft1"]
            )
            assert result["ok"] is True
            assert result["deleted"] == []
            reasons = {s["path"]: s["reason"] for s in result["skipped"]}
            assert reasons["/obj/other/scene_node"] == (
                "not a node recorded for this run"
            )
            assert "still referenced by" in reasons["/obj/eee_scratch_run1/draft1"]
            # Nothing reached the bridge delete paths.
            assert provider.calls == []
            assert provider.delete_calls == []
        finally:
            await db.close()

    asyncio.run(scenario())


def test_cleanup_nodes_tool_input_validation() -> None:
    context = RuntimeToolContext(
        read_only=_read_only_provider(),
        knowledge=_FakeKnowledge(),
    )
    runtime = _runtime_with_context(context)
    result = asyncio.run(cleanup_nodes.coroutine(runtime=runtime))
    assert result["ok"] is False
    assert result["code"] == "cleanup.context_invalid"
```

(The file already has `_runtime_with_context`, `_read_only_provider()`, and `_FakeKnowledge` helpers — reuse them; do not redefine. Import `cleanup_nodes` from `eee_agent.modeling.scratch_coordinator` alongside `scratch_build` at the top of the file.)

- [ ] **Step 8.2 — Run; expect failures.** `uv run --frozen --extra eval pytest -q tests/runtime/test_scratch_bridge.py -k "cleanup"` → fails (`AttributeError: 'ScratchCoordinator' object has no attribute 'cleanup'`).

- [ ] **Step 8.3 — Implement.**
  - `eee_agent/modeling/scratch_coordinator.py`:
    - Imports: add `ScratchDeleteResult`, `ScratchTopologyResult` to the scratch import; add `from eee_agent.runtime.task_graph import TaskGraphToolContext` (Task 9 needs it; adding now keeps imports together).
    - `ScratchProvider` Protocol: append

      ```python
          async def delete_nodes(
              self,
              *,
              paths: tuple[str, ...],
              allowed_paths: tuple[str, ...],
          ) -> ScratchDeleteResult: ...

          async def scene_topology(
              self,
              *,
              paths: tuple[str, ...],
          ) -> ScratchTopologyResult: ...
      ```

      (Runtime-checkable Protocols only verify method presence, so existing fakes without the new methods keep passing isinstance checks; the new fakes in Step 8.1 implement them.)
    - `ScratchCoordinator`: append the cleanup methods:

      ```python
          async def cleanup(self, *, paths: list[str] | None = None) -> dict[str, object]:
              """Two-phase abandoned-node cleanup: suggest, then execute.

              Driven by the task graph (what this run created) plus live scene
              topology (what is still referenced). Fail-closed: any failed
              check skips the node with a reason; nothing is silently deleted.
              """
              store = self._context.task_store
              if store is None or self._store_failed:
                  return {
                      "ok": False,
                      "code": "cleanup.store_unavailable",
                      "message": "The task graph is unavailable for this run.",
                  }
              run_id = self._context.run_id
              steps = await store.list_run_steps(run_id)
              live = [
                  (step, node)
                  for step in steps
                  for node in step.nodes
                  if node.status != "deleted"
              ]
              if paths is None:
                  return await self._cleanup_suggest(live)
              return await self._cleanup_execute(store, run_id, live, paths)

          async def _cleanup_suggest(self, live) -> dict[str, object]:
              """Phase 1: list deletion candidates; delete nothing."""
              if not live:
                  return {"ok": True, "phase": "suggest", "candidates": []}
              scene_paths = tuple(
                  node.committed_path or node.node_path for _, node in live
              )
              try:
                  topo = await self._context.provider.scene_topology(paths=scene_paths)
              except Exception as exc:  # noqa: BLE001 - bounded at this seam
                  return _bridge_or_op_failure(exc, default="cleanup.topology_failed")
              info = {entry["path"]: entry for entry in topo.nodes}
              candidates: list[dict[str, object]] = []
              for _step, node in live:
                  scene_path = node.committed_path or node.node_path
                  entry = info.get(scene_path)
                  if entry is None or not entry["exists"]:
                      candidates.append({
                          "path": scene_path,
                          "reason": "no longer exists in the scene",
                      })
                  elif not entry["outputs"]:
                      candidates.append({
                          "path": scene_path,
                          "reason": "nothing is wired downstream of it",
                      })
              return {"ok": True, "phase": "suggest", "candidates": candidates}

          async def _cleanup_execute(self, store, run_id, live, paths) -> dict[str, object]:
              """Phase 2: re-validate, then delete the requested paths."""
              requested: list[str] = []
              for path in paths:
                  if type(path) is not str or not path.startswith("/"):
                      return {
                          "ok": False,
                          "code": "cleanup.input_invalid",
                          "message": "paths must contain absolute node paths.",
                      }
                  requested.append(path)
              by_scene_path = {
                  (node.committed_path or node.node_path): node for _, node in live
              }
              deleted: list[str] = []
              skipped: list[dict[str, object]] = []
              sandbox_targets: list[str] = []
              committed_targets: list[str] = []
              for path in requested:
                  node = by_scene_path.get(path)
                  if node is None:
                      skipped.append({
                          "path": path,
                          "reason": "not a node recorded for this run",
                      })
                  elif node.status == "sandbox":
                      sandbox_targets.append(path)
                  else:
                      committed_targets.append(path)
              targets = sandbox_targets + committed_targets
              if targets:
                  try:
                      topo = await self._context.provider.scene_topology(
                          paths=tuple(targets)
                      )
                  except Exception as exc:  # noqa: BLE001 - bounded at this seam
                      return _bridge_or_op_failure(exc, default="cleanup.topology_failed")
                  delete_set = set(targets)
                  safe: set[str] = set()
                  for entry in topo.nodes:
                      if not entry["exists"]:
                          skipped.append({
                              "path": entry["path"],
                              "reason": "no longer exists in the scene",
                          })
                          continue
                      external = [o for o in entry["outputs"] if o not in delete_set]
                      if external:
                          skipped.append({
                              "path": entry["path"],
                              "reason": f"still referenced by {external[0]}",
                          })
                          continue
                      safe.add(entry["path"])
                  sandbox_targets = [p for p in sandbox_targets if p in safe]
                  committed_targets = [p for p in committed_targets if p in safe]
              if sandbox_targets:
                  ops = tuple(
                      ScratchOp(kind="delete_node", node_name=p.rsplit("/", 1)[-1])
                      for p in sandbox_targets
                  )
                  try:
                      await self._context.provider.scratch_exec(
                          sandbox_id=self._context.sandbox_id,
                          operations=ops,
                          purpose="cleanup: delete abandoned sandbox nodes",
                          preserve_on_failure=True,
                      )
                  except Exception as exc:  # noqa: BLE001 - bounded at this seam
                      return _bridge_or_op_failure(exc, default="cleanup.delete_failed")
                  deleted.extend(sandbox_targets)
              if committed_targets:
                  allowed = tuple(
                      node.committed_path
                      for _, node in live
                      if node.status == "committed" and node.committed_path
                  )
                  try:
                      result = await self._context.provider.delete_nodes(
                          paths=tuple(committed_targets),
                          allowed_paths=allowed,
                      )
                  except Exception as exc:  # noqa: BLE001 - bounded at this seam
                      return _bridge_or_op_failure(exc, default="cleanup.delete_failed")
                  deleted.extend(result.deleted_paths)
                  skipped.extend(dict(s) for s in result.skipped)
              if deleted:
                  await store.mark_deleted(run_id=run_id, node_paths=deleted)
              return {
                  "ok": True,
                  "phase": "execute",
                  "deleted": deleted,
                  "skipped": skipped,
              }
      ```

    - The `cleanup_nodes` tool (after `scratch_commit`):

      ```python
      @tool
      async def cleanup_nodes(
          runtime: ToolRuntime,
          paths: list[str] | None = None,
      ) -> dict[str, object]:
          """Suggest or execute deletion of abandoned nodes created by this run.

          Two phases. Call with NO paths first: returns the nodes this run
          created that are safe to delete (nothing is wired downstream of them,
          or they no longer exist), each with a reason — nothing is deleted.
          Then call again with paths=[...] chosen from the candidates to delete
          them. Deletion is fail-closed: a node still referenced by anything
          outside the delete set, or not created by this run, is skipped with a
          reason. Sandbox nodes are removed via the sandbox; committed nodes are
          deleted through an allowlist the runtime derives from its task graph.
          The whole deletion is a single undoable step in Houdini.

          Returns:
            suggest: {ok, phase: "suggest", candidates: [{path, reason}]}
            execute: {ok, phase: "execute", deleted: [...], skipped: [{path, reason}]}
          """
          context = getattr(runtime, "context", None)
          if type(context) is not RuntimeToolContext:
              return {
                  "ok": False,
                  "code": "cleanup.context_invalid",
                  "message": "A trusted scratch context is unavailable.",
              }
          scratch_context = getattr(context, "scratch", None)
          if type(scratch_context) is not ScratchToolContext:
              return {
                  "ok": False,
                  "code": "cleanup.context_invalid",
                  "message": "A trusted scratch context is unavailable.",
              }
          if paths is not None and (
              type(paths) is not list
              or not paths
              or len(paths) > 64
              or any(type(p) is not str for p in paths)
          ):
              return {
                  "ok": False,
                  "code": "cleanup.input_invalid",
                  "message": "paths must be a list of 1..64 node paths.",
              }
          return await scratch_context.coordinator.cleanup(paths=paths)
      ```

    - Add `"cleanup_nodes"` to `__all__`.
  - `eee_agent/runtime/agent_runner.py`: in `build_agent_runner`, extend the modeling import and registration:

    ```python
        from eee_agent.modeling.scratch_coordinator import (
            cleanup_nodes,
            scratch_build,
            scratch_commit,
        )

        tools.append(scratch_build)
        tools.append(scratch_commit)
        tools.append(cleanup_nodes)
    ```

- [ ] **Step 8.4 — Run; expect pass.** `uv run --frozen --extra eval pytest -q tests/runtime/test_scratch_bridge.py -k "cleanup"` → pass; then `uv run --frozen --extra eval pytest -q tests/runtime` → pass.

- [ ] **Step 8.5 — Commit.**
  `git add eee_agent/modeling/scratch_coordinator.py eee_agent/runtime/agent_runner.py tests/runtime/test_scratch_bridge.py`
  `git commit -m "feat(modeling): two-phase cleanup_nodes tool (task graph + live topology, fail-closed)"`

---

## Task 9 — `task_graph_status` tool + minimal read-only panel view

**Files:**
- Modify: `eee_agent/modeling/scratch_coordinator.py` (new `task_graph_status` tool; `__all__`)
- Modify: `eee_agent/runtime/agent_runner.py` (modeling tool registration)
- Modify: `eee_agent/runtime/protocol.py` (`COMMAND_TYPES` lines 27-51)
- Modify: `eee_agent/runtime/server.py` (after the `changeset.list` arm lines 567-579)
- Modify: `eee_agent/runtime/service.py` (new `list_task_steps` near the other list services)
- Modify: `eee_agent/panel/runtime_state.py` (after `parse_changeset_list` line 803)
- Create: `houdini_side/runtime_panel/task_graph_panel.py`
- Modify: `houdini_side/runtime_panel/client.py` (signal + `refresh_task_graph` + response purpose, mirroring `refresh_changesets` lines 641-650 / 915-922)
- Modify: `houdini_side/runtime_panel/conversation.py` (after line 165)
- Modify: `houdini_side/runtime_panel/main_window.py` (wire `taskGraphChanged` like the changesets signal)
- Test: `tests/runtime/test_task_graph_panel.py` (create)

The server command, service method, tool, and Qt-free parser are unit-tested offline. **The PySide6 widget and its wiring are manual-verify** (no Qt in the test venv; `runtime_panel/__init__.py` lazy-imports Qt modules for exactly this reason) — verified in Task 10.

- [ ] **Step 9.1 — Write the failing tests.** Create `tests/runtime/test_task_graph_panel.py`:

```python
"""task_graph_status tool + task_graph.list parser/service tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from eee_agent.modeling.scratch_coordinator import task_graph_status
from eee_agent.panel.runtime_state import (
    PanelClientError,
    format_task_graph_steps,
    parse_task_graph_list,
)
from eee_agent.runtime.agent_context import RuntimeToolContext
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.service import RuntimeService
from eee_agent.runtime.task_graph import TaskGraphStore, TaskGraphToolContext


def _run(coro):
    return asyncio.run(coro)


class _FakeReadOnly:
    async def scene_status(self):
        return {}

    async def query_scene(self, node_paths):
        return {}

    async def inspect_workspace(self, workspace_id):
        return {}

    async def geometry_stats(self, node_path):
        return {}

    async def work_status(self, workspace_id):
        return {}


class _FakeKnowledge:
    def search(self, query, *, limit=5):
        return {}

    def get(self, entity_id, *, max_body_bytes=8000):
        return {}


async def _open_with_step(db_path: Path) -> tuple[RuntimeDatabase, TaskGraphStore]:
    db = await RuntimeDatabase.open(db_path)
    now = datetime.now(timezone.utc).isoformat()
    async with db.write_transaction() as conn:
        await conn.execute(
            "INSERT INTO sessions(session_id, title, status, created_at, "
            "updated_at, last_seq, replay_floor_seq) "
            "VALUES ('sess_1', 't', 'active', ?, ?, 0, 0)",
            (now, now),
        )
        await conn.execute(
            "INSERT INTO runs(run_id, session_id, status, user_input, "
            "created_at, model_snapshot_json) "
            "VALUES ('run_1', 'sess_1', 'Planning', 'build', ?, '{}')",
            (now,),
        )
    store = TaskGraphStore(db)
    await store.record_step(run_id="run_1", tool="scratch_build", purpose="桌腿")
    return db, store


def test_task_graph_status_tool_returns_steps(tmp_path: Path) -> None:
    async def scenario() -> None:
        db, store = await _open_with_step(tmp_path / "app.sqlite")
        try:
            context = RuntimeToolContext(
                read_only=_FakeReadOnly(),
                knowledge=_FakeKnowledge(),
                task_graph=TaskGraphToolContext(store=store, run_id="run_1"),
            )
            runtime = SimpleNamespace(context=context)
            result = await task_graph_status.coroutine(runtime=runtime)
            assert result["ok"] is True
            assert result["run_id"] == "run_1"
            assert result["steps"] == [
                {
                    "seq": 1,
                    "tool": "scratch_build",
                    "purpose": "桌腿",
                    "status": "open",
                    "node_count": 0,
                }
            ]
        finally:
            await db.close()

    _run(scenario())


def test_task_graph_status_tool_without_context() -> None:
    runtime = SimpleNamespace(context=object())
    result = _run(task_graph_status.coroutine(runtime=runtime))
    assert result["ok"] is False
    assert result["code"] == "task_graph.context_invalid"


def test_service_list_task_steps(tmp_path: Path) -> None:
    async def scenario() -> None:
        db, store = await _open_with_step(tmp_path / "app.sqlite")
        try:
            service = object.__new__(RuntimeService)
            service._task_store = store
            result = await service.list_task_steps("run_1")
            assert len(result) == 1
            assert result[0]["purpose"] == "桌腿"
            assert result[0]["status"] == "open"
            assert await service.list_task_steps("run_missing") == []
        finally:
            await db.close()

    _run(scenario())


def test_parse_task_graph_list_valid() -> None:
    result = {
        "steps": [
            {"seq": 1, "tool": "scratch_build", "purpose": "桌腿",
             "status": "committed", "node_count": 2},
        ]
    }
    parsed = parse_task_graph_list(result)
    assert len(parsed) == 1
    text = format_task_graph_steps(parsed)
    assert text == "1. [committed] 桌腿 (2 nodes)"


def test_parse_task_graph_list_empty_formats_placeholder() -> None:
    assert parse_task_graph_list({"steps": []}) == ()
    assert format_task_graph_steps(()) == "No build steps recorded for this run."


def test_parse_task_graph_list_rejects_malformed() -> None:
    with pytest.raises(PanelClientError):
        parse_task_graph_list({"steps": "nope"})
    with pytest.raises(PanelClientError):
        parse_task_graph_list({"steps": [{"seq": 1}]})
    with pytest.raises(PanelClientError):
        parse_task_graph_list(
            {"steps": [{"seq": 1, "tool": "scratch_build", "purpose": "x",
                        "status": "weird", "node_count": 0}]}
        )
```

- [ ] **Step 9.2 — Run; expect failures.** `uv run --frozen --extra eval pytest -q tests/runtime/test_task_graph_panel.py` → fails (no `task_graph_status` tool / `parse_task_graph_list` / `list_task_steps`).

- [ ] **Step 9.3 — Implement the tool + registration.**
  - `eee_agent/modeling/scratch_coordinator.py`, after `cleanup_nodes`:

    ```python
    @tool
    async def task_graph_status(runtime: ToolRuntime) -> dict[str, object]:
        """Return the current run's recorded build steps (read-only).

        Each step: seq, tool, purpose, status (open/committed/deleted), and how
        many nodes it created. Use this to recall what you have already built
        before deciding the next step or a cleanup.
        """
        context = getattr(runtime, "context", None)
        if type(context) is not RuntimeToolContext:
            return {
                "ok": False,
                "code": "task_graph.context_invalid",
                "message": "A trusted task graph context is unavailable.",
            }
        task_graph = getattr(context, "task_graph", None)
        if type(task_graph) is not TaskGraphToolContext:
            return {
                "ok": False,
                "code": "task_graph.unavailable",
                "message": "The task graph is unavailable for this run.",
            }
        try:
            steps = await task_graph.store.list_run_steps(task_graph.run_id)
        except Exception:  # noqa: BLE001 - bounded at this seam
            return {
                "ok": False,
                "code": "task_graph.unavailable",
                "message": "The task graph could not be read.",
            }
        return {
            "ok": True,
            "run_id": task_graph.run_id,
            "steps": [
                {
                    "seq": step.seq,
                    "tool": step.tool,
                    "purpose": step.purpose,
                    "status": step.status,
                    "node_count": len(step.nodes),
                }
                for step in steps
            ],
        }
    ```

    Add `"task_graph_status"` to `__all__`.
  - `eee_agent/runtime/agent_runner.py`: add `task_graph_status` to the modeling import and `tools.append(task_graph_status)` after `tools.append(cleanup_nodes)`.

- [ ] **Step 9.4 — Implement the server command path.**
  - `eee_agent/runtime/protocol.py`: add `"task_graph.list",` to `COMMAND_TYPES` (keep the set's alphabetical grouping).
  - `eee_agent/runtime/server.py`: after the `changeset.list` arm, add:

    ```python
            if ct == "task_graph.list":
                _validate(payload, {"run_id": _is_str})
                result = await self._service.list_task_steps(payload["run_id"])
                self._put(ctx, success_response(req, {"steps": result}))
                return
    ```

  - `eee_agent/runtime/service.py`: add near the other list services:

    ```python
        async def list_task_steps(self, run_id: str) -> list[dict[str, object]]:
            """Bounded per-run task step list for the panel (read-only).

            Returns at most the newest 50 steps; an unknown run yields an
            empty list (a panel may poll a run id that recorded nothing).
            """
            store = getattr(self, "_task_store", None)
            if store is None or type(run_id) is not str or not run_id:
                return []
            steps = await store.list_run_steps(run_id)
            return [
                {
                    "seq": step.seq,
                    "tool": step.tool,
                    "purpose": step.purpose,
                    "status": step.status,
                    "node_count": len(step.nodes),
                }
                for step in steps[-50:]
            ]
    ```

- [ ] **Step 9.5 — Implement the Qt-free parser in `eee_agent/panel/runtime_state.py`** (after `parse_changeset_list`):

    ```python
    _TASK_STEP_FIELDS = frozenset({"seq", "tool", "purpose", "status", "node_count"})
    _TASK_STEP_STATUSES = frozenset({"open", "committed", "deleted"})


    def parse_task_graph_list(result: object) -> tuple[Mapping[str, object], ...]:
        """Validate the exact bounded `task_graph.list` result."""
        if type(result) is not dict or set(result) != {"steps"}:
            raise PanelClientError("Runtime task graph list is invalid.")
        items = result["steps"]
        if type(items) is not list or len(items) > 50:
            raise PanelClientError("Runtime task graph list is invalid.")
        parsed: list[Mapping[str, object]] = []
        for item in items:
            if type(item) is not dict or set(item) != _TASK_STEP_FIELDS:
                raise PanelClientError("Runtime task graph list is invalid.")
            if type(item["seq"]) is not int or item["seq"] < 1:
                raise PanelClientError("Runtime task graph list is invalid.")
            if type(item["tool"]) is not str or type(item["purpose"]) is not str:
                raise PanelClientError("Runtime task graph list is invalid.")
            if item["status"] not in _TASK_STEP_STATUSES:
                raise PanelClientError("Runtime task graph list is invalid.")
            if type(item["node_count"]) is not int or item["node_count"] < 0:
                raise PanelClientError("Runtime task graph list is invalid.")
            parsed.append(item)
        return tuple(parsed)


    def format_task_graph_steps(steps: tuple[Mapping[str, object], ...]) -> str:
        """Render the validated step list as plain text for the panel block."""
        if not steps:
            return "No build steps recorded for this run."
        return "\n".join(
            f"{step['seq']}. [{step['status']}] {step['purpose']} "
            f"({step['node_count']} nodes)"
            for step in steps
        )
    ```

- [ ] **Step 9.6 — Run; expect pass.** `uv run --frozen --extra eval pytest -q tests/runtime/test_task_graph_panel.py tests/panel` → all pass.

- [ ] **Step 9.7 — Implement the panel widget + wiring (manual-verify).**
  - Create `houdini_side/runtime_panel/task_graph_panel.py`:

    ```python
    """Read-only task-graph text block for the Runtime panel.

    Shows the current run's recorded build steps (seq / status / purpose /
    node count) as plain text. No graph rendering and no editing — the
    graphical task map is a later round. All data arrives already validated
    via ``eee_agent.panel.runtime_state.parse_task_graph_list``.
    """

    from __future__ import annotations

    from PySide6 import QtWidgets


    class TaskGraphPanel(QtWidgets.QWidget):
        """A minimal read-only text view fed by ``task_graph.list`` results."""

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            self._title = QtWidgets.QLabel("Task graph")
            layout.addWidget(self._title)
            self._view = QtWidgets.QTextEdit()
            self._view.setReadOnly(True)
            self._view.setMaximumHeight(160)
            layout.addWidget(self._view)
            self.set_steps(())

        def set_steps(self, steps) -> None:
            from eee_agent.panel.runtime_state import format_task_graph_steps

            self._view.setPlainText(format_task_graph_steps(tuple(steps)))
    ```

  - `houdini_side/runtime_panel/client.py`: add `taskGraphChanged = QtCore.Signal(tuple)` next to `changesetsChanged`; add (mirroring `refresh_changesets`):

    ```python
        def refresh_task_graph(self) -> None:
            run_id = getattr(self, "_current_run_id", None)
            if run_id is None:
                self.taskGraphChanged.emit(())
                return
            self._send("task_graph.list", {"run_id": run_id}, "task_graph.list")
    ```

    and in the response handler (next to the `changeset.list` purpose arm):

    ```python
            if purpose == "task_graph.list":
                try:
                    items = parse_task_graph_list(result)
                except PanelClientError as exc:
                    self.connectionChanged.emit("error", str(exc))
                    return
                self.taskGraphChanged.emit(items)
                return
    ```

    (Import `parse_task_graph_list` alongside `parse_changeset_list`. If the active-run tracking attribute is named differently than `_current_run_id`, use the existing one — check how the client learns the current run from run events; this is part of the manual-verify pass.)
  - Call `self.refresh_task_graph()` wherever the client already schedules `refresh_changesets()` (run lifecycle events), and on WebSocket reconnect.
  - `houdini_side/runtime_panel/conversation.py`: after `layout.addWidget(self.thinking_panel)` add:

    ```python
            from houdini_side.runtime_panel.task_graph_panel import TaskGraphPanel
            self.task_graph_panel = TaskGraphPanel(self)
            layout.addWidget(self.task_graph_panel)
    ```

  - `houdini_side/runtime_panel/main_window.py`: connect `self._client.taskGraphChanged` to `self.conversation.task_graph_panel.set_steps` next to the existing `changesetsChanged` wiring.

- [ ] **Step 9.8 — Verify no offline regressions, then commit.** `uv run --frozen --extra eval pytest -q tests/runtime tests/panel` → pass (Qt modules are lazy-imported and never loaded offline).
  `git add eee_agent/modeling/scratch_coordinator.py eee_agent/runtime/agent_runner.py eee_agent/runtime/protocol.py eee_agent/runtime/server.py eee_agent/runtime/service.py eee_agent/panel/runtime_state.py houdini_side/runtime_panel/task_graph_panel.py houdini_side/runtime_panel/client.py houdini_side/runtime_panel/conversation.py houdini_side/runtime_panel/main_window.py tests/runtime/test_task_graph_panel.py`
  `git commit -m "feat(runtime): task_graph_status tool + task_graph.list command + read-only panel block"`

---

## Task 10 — hython smoke + documentation + full offline gate

**Files:**
- Create: `tests/runtime/task_graph_houdini_smoke.py`
- Modify: `CLAUDE.md` (handoff section, agent boundary list, runtime status)
- Modify: `docs/superpowers/specs/2026-07-24-node-lifecycle-task-graph-design.md` (append 实现注记）
- Create: `docs/handoffs/<date>-node-lifecycle-task-graph-handoff.md` (per spec §6)

- [ ] **Step 10.1 — Write the hython smoke.** Create `tests/runtime/task_graph_houdini_smoke.py`:

```python
"""Node lifecycle real-Houdini smoke (run with hython, NOT pytest).

Exercises the commit-finalization and cleanup executor surfaces against the
live ``hou`` module:

1. scratch_exec builds box1 -> xform1 in the sandbox (with a purpose);
2. scratch_commit promotes with annotations; then asserts:
   - committed nodes moved off their default positions and are layered
     (xform1 exactly one column right of box1);
   - the output node has the display AND render flags set;
   - box1 carries the annotation comment;
3. an orphan node is created, scratch_topology reports no downstream
   consumers, and delete_nodes removes it through the allowlist;
4. everything the smoke created is destroyed on exit; the user's HIP is
   never saved, loaded, or cleared.

Run (detected hython)::

    "<Houdini>/bin/hython.exe" tests/runtime/task_graph_houdini_smoke.py

Exit code 0 on success; non-zero with a message on any failure. The module is
imported by hython only (``hou`` is unavailable under pytest/the venv).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VENV_SITE_PACKAGES = _REPO_ROOT / ".venv" / "Lib" / "site-packages"
for _path in (_REPO_ROOT, _VENV_SITE_PACKAGES):
    if _path.exists() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _die(message: str) -> None:
    print(f"SMOKE FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    try:
        import hou  # noqa: F401  — only available inside hython
    except ImportError:
        _die("hou is not importable; run this script with hython, not pytest/python.")

    from eee_agent.houdini_bridge.scratch import (
        ScratchCommitRequest,
        ScratchDeleteRequest,
        ScratchOp,
        ScratchRequest,
        ScratchTopologyRequest,
    )
    from houdini_side.changeset_executor import ChangeSetExecutor
    from houdini_side.secure_bridge import create_houdini_scene_adapter

    adapter = create_houdini_scene_adapter()
    executor = ChangeSetExecutor(adapter)
    epoch = executor.binding().scene_epoch

    TARGET = "/obj/eee_taskgraph_smoke"
    pre = hou.node(TARGET)
    if pre is not None:
        pre.destroy()
    try:
        build = ScratchRequest.build(
            request_id="smoke_build",
            deadline_ms=30000,
            scene_epoch=epoch,
            sandbox_id="taskgraph_smoke",
            operations=(
                ScratchOp(kind="create_node", node_name="box1", node_type="box",
                          note="smoke 盒体"),
                ScratchOp(kind="create_node", node_name="xform1", node_type="xform"),
                ScratchOp(kind="connect", node_name="xform1", input_index=0,
                          source="box1", source_output_index=0),
            ),
            purpose="smoke: build a box through an xform",
        )
        built = executor.scratch_exec(build)
        if built.applied_ops != 3:
            _die(f"build applied {built.applied_ops} ops, expected 3")

        commit = ScratchCommitRequest.build(
            request_id="smoke_commit",
            deadline_ms=30000,
            scene_epoch=epoch,
            sandbox_id="taskgraph_smoke",
            target_parent_path="/obj",
            target_name="eee_taskgraph_smoke",
            annotations={"box1": "smoke 注释"},
        )
        verdict = executor.scratch_commit(commit)
        if not verdict.committed:
            _die(f"commit refused: {verdict.reason}")

        box = hou.node(f"{TARGET}/box1")
        xform = hou.node(f"{TARGET}/xform1")
        if box is None or xform is None:
            _die("committed nodes missing after promotion")
        bx, by = (float(v) for v in box.position())
        xx, xy = (float(v) for v in xform.position())
        if abs((xx - bx) - 3.0) > 1e-6 or abs(xy - by) > 1e-6:
            _die(f"layout not layered: box at {(bx, by)}, xform at {(xx, xy)}")
        if not xform.isDisplayFlagSet():
            _die("display flag not set on the committed output node")
        if not xform.isRenderFlagSet():
            _die("render flag not set on the committed SOP output node")
        if box.comment() != "smoke 注释":
            _die(f"annotation comment not written: {box.comment()!r}")
        print("commit finalization: layout + flags + comment OK")

        orphan = hou.node(TARGET).createNode("null", "orphan1")
        topology = executor.scratch_topology(
            ScratchTopologyRequest.build(
                request_id="smoke_topo",
                deadline_ms=30000,
                scene_epoch=epoch,
                paths=(f"{TARGET}/orphan1",),
            )
        )
        entry = topology.nodes[0]
        if not entry["exists"] or entry["outputs"]:
            _die(f"topology wrong for orphan: {entry}")
        deleted = executor.delete_nodes(
            ScratchDeleteRequest.build(
                request_id="smoke_delete",
                deadline_ms=30000,
                scene_epoch=epoch,
                allowed_paths=(f"{TARGET}/orphan1",),
                paths=(f"{TARGET}/orphan1",),
            )
        )
        if deleted.deleted_paths != (f"{TARGET}/orphan1",):
            _die(f"delete_nodes verdict wrong: {deleted}")
        if hou.node(f"{TARGET}/orphan1") is not None:
            _die("orphan node still present after delete_nodes")
        print("topology + allowlisted delete OK")
        print("SMOKE OK: node lifecycle (finalize + topology + delete)")
    finally:
        cleanup = hou.node(TARGET)
        if cleanup is not None:
            cleanup.destroy()
            print(f"cleanup: destroyed {TARGET}")


if __name__ == "__main__":
    if "PYTEST" in "".join(sys.argv).upper() or "pytest" in sys.argv[0]:
        _die("this smoke runs under hython, not pytest")
    main()
```

- [ ] **Step 10.2 — Run the smoke in real Houdini (manual acceptance).** Probe the install with `scripts/env_probe.sh`, then `"<Houdini>/bin/hython.exe" tests/runtime/task_graph_houdini_smoke.py` → `SMOKE OK`. Record the result in the handoff doc. Also manually verify the panel task-graph block (Task 9, assumption 8): open the Runtime panel in Houdini, run a build, confirm the read-only "Task graph" block lists steps.

- [ ] **Step 10.3 — Update `CLAUDE.md`.**
  - In "Current development handoff", add a paragraph: node lifecycle & task graph merged (schema v8, `scratch.v2` capability, commit auto-finalization, `cleanup_nodes`/`task_graph_status` tools, per-call task summary injection, panel task block), with the new full-gate count.
  - In "Agent boundary" (Secure Bridge and Runtime Control section): extend the modeling tool list to "Modeling runs additionally expose `scratch_build` (with a required per-call `purpose`), `scratch_commit`, `cleanup_nodes` (two-phase废弃-node cleanup), and `task_graph_status`."
  - Add to "Critical gotchas": "The read-only inspector test whitelist forbidding `setDisplayFlag`/`setRenderFlag` scopes the read-only inspector tool only; the commit write path sets display/render flags deliberately during `scratch_commit` finalization."
  - Update the Layout section: `eee_agent/runtime` gains `task_graph` (per-run task graph store + summary); note `scratch.v2` in the bridge description.

- [ ] **Step 10.4 — Append implementation notes to the spec.** Add a `## 7. 实现注记` section to `docs/superpowers/specs/2026-07-24-node-lifecycle-task-graph-design.md` recording deviations 1-8 from the plan's "Assumptions & deviations" (topology op instead of scene.query extension, `scratch.v2` naming, `purpose` on the wire DTO, annotations as sorted pairs, `warnings` on commit result, anchor rule, suggest criterion, panel manual-verify).

- [ ] **Step 10.5 — Write the handoff doc** at `docs/handoffs/<today>-node-lifecycle-task-graph-handoff.md`: what shipped, schema v8, capability, tools, test counts, smoke result, and the manual panel verification outcome (per spec §6).

- [ ] **Step 10.6 — Full offline gate.** `uv run --frozen --extra eval pytest -q` → all pass, 0 failures (baseline was 3458 passed, 11 skipped; the new suites add ~60 tests). Also `uv run --frozen --extra eval python -m eee_agent.cli versions` still works.

- [ ] **Step 10.7 — Commit.**
  `git add tests/runtime/task_graph_houdini_smoke.py CLAUDE.md docs/superpowers/specs/2026-07-24-node-lifecycle-task-graph-design.md docs/handoffs/`
  `git commit -m "docs: node lifecycle & task graph smoke + CLAUDE.md + implementation notes"`

---

## Final verification checklist

- [ ] `uv run --frozen --extra eval pytest -q` — full gate green.
- [ ] `uv run --frozen --extra eval python -m eee_agent.cli versions` — version probe OK.
- [ ] hython smoke `tests/runtime/task_graph_houdini_smoke.py` — `SMOKE OK` (manual).
- [ ] Panel task-graph block shows current run steps in a live session (manual).
- [ ] `git log --oneline -12` shows the ten task commits in order.
