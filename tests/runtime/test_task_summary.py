"""Task summary middleware tests without an LLM or Houdini process."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from langchain_core.messages import SystemMessage

from eee_agent.runtime.agent_context import RuntimeToolContext
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.task_graph import TaskGraphStore, TaskGraphToolContext
from eee_agent.task_summary import TaskSummaryMiddleware


class _ReadOnly:
    async def scene_status(self): return {}
    async def query_scene(self, node_paths): return {}
    async def inspect_workspace(self, workspace_id): return {}
    async def geometry_stats(self, node_path): return {}
    async def work_status(self, workspace_id): return {}


class _Knowledge:
    def search(self, query, *, limit=5): return {}
    def get(self, entity_id, *, max_body_bytes=8000): return {}


class _Runtime:
    def __init__(self, context): self.context = context


class _Request:
    def __init__(self, context, system_message=None):
        self.runtime = _Runtime(context)
        self.system_message = system_message

    def override(self, *, system_message):
        return _Request(self.runtime.context, system_message)


def _context(task_graph=None):
    return RuntimeToolContext(
        read_only=_ReadOnly(), knowledge=_Knowledge(), task_graph=task_graph
    )


async def _db_with_step(path):
    db = await RuntimeDatabase.open(path)
    now = datetime.now(timezone.utc).isoformat()
    async with db.write_transaction() as conn:
        await conn.execute(
            "INSERT INTO sessions(session_id,title,status,created_at,updated_at,last_seq,replay_floor_seq) "
            "VALUES ('s','t','active',?,?,0,0)", (now, now)
        )
        await conn.execute(
            "INSERT INTO runs(run_id,session_id,status,user_input,created_at,model_snapshot_json) "
            "VALUES ('r','s','Planning','build',?,'{}')", (now,)
        )
    store = TaskGraphStore(db)
    step = await store.record_step(run_id="r", tool="scratch_build", purpose="build")
    await store.record_nodes(
        step_id=step.step_id, nodes=[("/obj/eee_scratch_r/box1", "box", None)]
    )
    return db, store


def test_injects_summary_and_passthrough_without_context(tmp_path):
    async def run():
        db, store = await _db_with_step(tmp_path / "app.sqlite")
        try:
            request = _Request(
                _context(TaskGraphToolContext(store=store, run_id="r")),
                SystemMessage(content="BASE"),
            )
            injected = await TaskSummaryMiddleware()._inject(request)
            assert "Task state (run r):" in injected.system_message.content
            plain = _Request(_context(), SystemMessage(content="BASE"))
            assert await TaskSummaryMiddleware()._inject(plain) is plain
        finally:
            await db.close()

    asyncio.run(run())


def test_store_failure_is_swallowed(tmp_path):
    async def run():
        db, store = await _db_with_step(tmp_path / "app.sqlite")
        await db.close()
        request = _Request(
            _context(TaskGraphToolContext(store=store, run_id="r")),
            SystemMessage(content="BASE"),
        )
        assert await TaskSummaryMiddleware()._inject(request) is request

    asyncio.run(run())
