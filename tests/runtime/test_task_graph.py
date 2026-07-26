"""Task graph store: migration, recording, lifecycle, and bounded summary."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.task_graph import TaskGraphStore, TaskGraphToolContext


def _run(coro):
    return asyncio.run(coro)


async def _open(path: Path) -> tuple[RuntimeDatabase, TaskGraphStore]:
    database = await RuntimeDatabase.open(path)
    return database, TaskGraphStore(database)


async def _seed(database: RuntimeDatabase, run_id: str = "run_1") -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with database.write_transaction() as conn:
        await conn.execute(
            "INSERT INTO sessions(session_id,title,status,created_at,updated_at,"
            "last_seq,replay_floor_seq) VALUES ('sess_1','t','active',?,?,0,0)",
            (now, now),
        )
        await conn.execute(
            "INSERT INTO runs(run_id,session_id,status,user_input,created_at,"
            "model_snapshot_json) VALUES (?,'sess_1','Planning','build',?,'{}')",
            (run_id, now),
        )


def test_schema_v8_creates_task_tables(db_path: Path) -> None:
    async def scenario() -> None:
        database, _ = await _open(db_path)
        try:
            assert await database.schema_version() == 8
            assert {"task_steps", "task_nodes"} <= await database.table_names()
        finally:
            await database.close()

    _run(scenario())


def test_record_list_commit_delete_and_summary(db_path: Path) -> None:
    async def scenario() -> None:
        database, store = await _open(db_path)
        try:
            await _seed(database)
            first = await store.record_step(
                run_id="run_1", tool="scratch_build", purpose="创建桌腿"
            )
            second = await store.record_step(
                run_id="run_1", tool="scratch_build", purpose="创建桌面"
            )
            assert (first.seq, second.seq) == (1, 2)
            await store.record_nodes(
                step_id=first.step_id,
                nodes=[
                    ("/obj/eee_scratch_run_1/leg1", "box", "左前腿"),
                    ("/obj/eee_scratch_run_1/leg2", "box", None),
                ],
            )
            await store.record_nodes(
                step_id=second.step_id,
                nodes=[("/obj/eee_scratch_run_1/tabletop", "box", "输出")],
            )
            await store.mark_committed(
                run_id="run_1",
                sandbox_root="/obj/eee_scratch_run_1",
                final_path="/obj/table1",
            )
            steps = await store.list_run_steps("run_1")
            assert steps[0].nodes[0].committed_path == "/obj/table1/leg1"
            assert all(step.status == "committed" for step in steps)
            summary = await store.render_summary("run_1")
            assert summary is not None
            assert "创建桌面 (tabletop)" in summary
            assert "Current output: /obj/table1/tabletop" in summary
            assert (
                await store.mark_deleted(
                    run_id="run_1",
                    node_paths=["/obj/table1/leg1", "/obj/table1/leg2"],
                )
                == 2
            )
            assert (
                await store.mark_deleted(
                    run_id="run_1", node_paths=["/obj/table1/leg1"]
                )
                == 0
            )
            steps = await store.list_run_steps("run_1")
            assert steps[0].status == "deleted"
            assert steps[1].status == "committed"
        finally:
            await database.close()

    _run(scenario())


def test_validation_and_sandbox_containment(db_path: Path) -> None:
    async def scenario() -> None:
        database, store = await _open(db_path)
        try:
            await _seed(database)
            with pytest.raises(ValueError):
                await store.record_step(
                    run_id="run_1", tool="move_node", purpose="x"
                )
            with pytest.raises(ValueError):
                await store.record_step(
                    run_id="run_1", tool="scratch_build", purpose=" "
                )
            step = await store.record_step(
                run_id="run_1", tool="scratch_build", purpose="valid"
            )
            with pytest.raises(ValueError):
                await store.record_nodes(
                    step_id=step.step_id,
                    nodes=[("../escape", "box", None)],
                )
            await store.record_nodes(
                step_id=step.step_id,
                nodes=[("/obj/other/box1", "box", None)],
            )
            with pytest.raises(ValueError, match="outside"):
                await store.mark_committed(
                    run_id="run_1",
                    sandbox_root="/obj/eee_scratch_run_1",
                    final_path="/obj/table",
                )
        finally:
            await database.close()

    _run(scenario())


def test_summary_is_recent_and_bounded(db_path: Path) -> None:
    async def scenario() -> None:
        database, store = await _open(db_path)
        try:
            await _seed(database)
            assert await store.render_summary("run_1") is None
            for index in range(20):
                await store.record_step(
                    run_id="run_1",
                    tool="scratch_build",
                    purpose=f"步骤{index} " + "长" * 100,
                )
            summary = await store.render_summary("run_1")
            assert summary is not None
            assert len(summary.encode("utf-8")) <= 2048
            assert "earlier step(s) omitted" in summary
            assert "步骤19" in summary
        finally:
            await database.close()

    _run(scenario())


def test_context_requires_exact_store(db_path: Path) -> None:
    async def scenario() -> None:
        database, store = await _open(db_path)
        try:
            assert TaskGraphToolContext(store, "run_1").run_id == "run_1"
            with pytest.raises(TypeError):
                TaskGraphToolContext(object(), "run_1")  # type: ignore[arg-type]
            with pytest.raises(TypeError):
                TaskGraphToolContext(store, "")
        finally:
            await database.close()

    _run(scenario())
