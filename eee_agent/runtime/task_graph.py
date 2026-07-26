"""Durable per-run task graph for scratch node ownership and lifecycle."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from eee_agent.runtime.database import RuntimeDatabase

_MAX_PURPOSE_CHARS = 200
_MAX_NOTE_CHARS = 200
_MAX_PATH_CHARS = 512
_SUMMARY_MAX_STEPS = 10
_SUMMARY_MAX_BYTES = 2048
_STEP_TOOLS = frozenset({"scratch_build", "scratch_commit", "cleanup_nodes"})

_STEP_COLUMNS = "step_id, run_id, seq, tool, purpose, status, created_at"
_NODE_COLUMNS = (
    "node_id, step_id, node_path, committed_path, node_type, note, status"
)


@dataclass(frozen=True, slots=True)
class TaskNode:
    node_id: int
    step_id: int
    node_path: str
    committed_path: str | None
    node_type: str
    note: str | None
    status: str


@dataclass(frozen=True, slots=True)
class TaskStep:
    step_id: int
    run_id: str
    seq: int
    tool: str
    purpose: str
    status: str
    created_at: datetime
    nodes: tuple[TaskNode, ...] = ()


def _require_run_id(run_id: object) -> str:
    if type(run_id) is not str or not run_id or len(run_id) > 128:
        raise ValueError("run_id must be a bounded non-empty string")
    return run_id


def _require_purpose(purpose: object) -> str:
    if type(purpose) is not str or not purpose.strip():
        raise ValueError("purpose must be a non-empty string")
    if len(purpose) > _MAX_PURPOSE_CHARS:
        raise ValueError(
            f"purpose must be at most {_MAX_PURPOSE_CHARS} characters"
        )
    return purpose


def _require_path(path: object, label: str) -> str:
    if (
        type(path) is not str
        or not path.startswith("/")
        or len(path) > _MAX_PATH_CHARS
        or "\\" in path
        or ".." in path.split("/")
    ):
        raise ValueError(f"{label} must be a bounded absolute node path")
    return path


class TaskGraphStore:
    """Persist step intent and runtime-owned node lifecycle without scene IO."""

    def __init__(self, database: RuntimeDatabase) -> None:
        if type(database) is not RuntimeDatabase:
            raise TypeError("database must be a RuntimeDatabase")
        self._database = database

    async def record_step(self, *, run_id: str, tool: str, purpose: str) -> TaskStep:
        rid = _require_run_id(run_id)
        if type(tool) is not str or tool not in _STEP_TOOLS:
            raise ValueError(f"tool must be one of {sorted(_STEP_TOOLS)}")
        checked_purpose = _require_purpose(purpose)
        now = datetime.now(timezone.utc)
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
                "FROM task_steps WHERE run_id = ?",
                (rid,),
            )
            seq = int((await cursor.fetchone())["next_seq"])
            cursor = await conn.execute(
                "INSERT INTO task_steps(run_id, seq, tool, purpose, status, created_at) "
                "VALUES (?, ?, ?, ?, 'open', ?)",
                (rid, seq, tool, checked_purpose, now.isoformat()),
            )
            step_id = cursor.lastrowid
        if type(step_id) is not int:
            raise RuntimeError("task step insert did not return an id")
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
        if type(step_id) is not int or step_id < 1:
            raise ValueError("step_id must be a positive integer")
        if not isinstance(nodes, Sequence) or len(nodes) > 64:
            raise ValueError("nodes must be a bounded sequence")
        prepared: list[tuple[str, str, str | None]] = []
        for item in nodes:
            if type(item) is not tuple or len(item) != 3:
                raise ValueError("each node must be a (path, type, note) tuple")
            path, node_type, note = item
            checked_path = _require_path(path, "node_path")
            if (
                type(node_type) is not str
                or not node_type
                or len(node_type) > 128
            ):
                raise ValueError("node_type must be a bounded non-empty string")
            if note is not None and (
                type(note) is not str or len(note) > _MAX_NOTE_CHARS
            ):
                raise ValueError(
                    f"note must be a string of at most {_MAX_NOTE_CHARS} characters"
                )
            prepared.append((checked_path, node_type, note))
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
                node_id = cursor.lastrowid
                if type(node_id) is not int:
                    raise RuntimeError("task node insert did not return an id")
                out.append(
                    TaskNode(
                        node_id=node_id,
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
        rid = _require_run_id(run_id)
        root = _require_path(sandbox_root, "sandbox_root").rstrip("/")
        if not root.startswith("/obj/eee_scratch_"):
            raise ValueError("sandbox_root must be a reserved scratch path")
        target = _require_path(final_path, "final_path").rstrip("/")
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT node_id, node_path FROM task_nodes "
                "WHERE run_id = ? AND status = 'sandbox'",
                (rid,),
            )
            for row in await cursor.fetchall():
                source = row["node_path"]
                if not source.startswith(root + "/"):
                    raise ValueError("recorded node is outside the run sandbox")
                suffix = source[len(root) :]
                await conn.execute(
                    "UPDATE task_nodes SET committed_path = ?, status = 'committed' "
                    "WHERE node_id = ?",
                    (target + suffix, row["node_id"]),
                )
            await conn.execute(
                "UPDATE task_steps SET status = 'committed' "
                "WHERE run_id = ? AND status = 'open'",
                (rid,),
            )

    async def mark_deleted(self, *, run_id: str, node_paths: Sequence[str]) -> int:
        rid = _require_run_id(run_id)
        if not isinstance(node_paths, Sequence) or len(node_paths) > 64:
            raise ValueError("node_paths must be a bounded sequence")
        paths = [_require_path(path, "node_path") for path in node_paths]
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
                "AND status != 'deleted' AND EXISTS ("
                "  SELECT 1 FROM task_nodes WHERE task_nodes.step_id = task_steps.step_id"
                ") AND NOT EXISTS ("
                "  SELECT 1 FROM task_nodes WHERE task_nodes.step_id = task_steps.step_id "
                "  AND task_nodes.status != 'deleted'"
                ")",
                (rid,),
            )
        return count

    async def list_run_steps(self, run_id: str) -> tuple[TaskStep, ...]:
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
        first_step_line = 2 if omitted else 1
        while (
            len("\n".join(lines).encode("utf-8")) > _SUMMARY_MAX_BYTES
            and len(lines) > first_step_line + 1
        ):
            del lines[first_step_line]
        encoded = "\n".join(lines).encode("utf-8")
        return encoded[:_SUMMARY_MAX_BYTES].decode("utf-8", errors="ignore")

    @staticmethod
    def _current_output(steps: tuple[TaskStep, ...]) -> str | None:
        for step in reversed(steps):
            for node in reversed(step.nodes):
                if node.status == "committed" and node.committed_path:
                    return node.committed_path
        return None


@dataclass(frozen=True, slots=True)
class TaskGraphToolContext:
    store: TaskGraphStore
    run_id: str

    def __post_init__(self) -> None:
        if type(self.store) is not TaskGraphStore:
            raise TypeError("TaskGraphToolContext.store must be a TaskGraphStore")
        try:
            _require_run_id(self.run_id)
        except ValueError as exc:
            raise TypeError(
                "TaskGraphToolContext.run_id must be a bounded non-empty string"
            ) from exc


__all__ = ["TaskGraphStore", "TaskGraphToolContext", "TaskNode", "TaskStep"]
