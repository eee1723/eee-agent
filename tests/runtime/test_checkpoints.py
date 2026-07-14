from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from eee_agent.runtime.checkpoints import CheckpointManager
from eee_agent.runtime.database import RuntimeDatabase


class _GraphState(TypedDict):
    value: int


async def _increment(state: _GraphState) -> _GraphState:
    return {"value": state["value"] + 1}


def _build_graph(checkpointer: AsyncSqliteSaver):
    builder = StateGraph(_GraphState)
    builder.add_node("increment", _increment)
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    return builder.compile(checkpointer=checkpointer)


def _run(coro):
    return asyncio.run(coro)


class _FakeSaver:
    async def setup(self) -> None:
        return None


class _BoomSetupSaver:
    async def setup(self) -> None:
        raise RuntimeError("setup boom")


class _FakeContext:
    """Records enter/exit for lifecycle edge tests (no real SQLite)."""

    def __init__(self, saver, log: list) -> None:
        self._saver = saver
        self._log = log

    async def __aenter__(self):
        self._log.append("enter")
        return self._saver

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._log.append(("exit", exc_type))
        return False


def _patch_from_conn_string(monkeypatch, context) -> None:
    monkeypatch.setattr(
        AsyncSqliteSaver,
        "from_conn_string",
        staticmethod(lambda _path: context),
    )


# --------------------------------------------------------------------------
# real LangGraph persistence
# --------------------------------------------------------------------------

def test_checkpoint_persists_across_reopen(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "checkpoints.sqlite"
        session_id = "ses_test"
        async with CheckpointManager(path) as manager:
            graph = _build_graph(manager.require_saver())
            cfg = {"configurable": {"thread_id": session_id}}
            await graph.ainvoke({"value": 1}, cfg)
            assert await manager.require_saver().aget(cfg) is not None
            assert path.exists()
        # closed: public state reset, require_saver refuses
        assert manager.saver is None
        with pytest.raises(RuntimeError):
            manager.require_saver()
        # reopen a fresh manager on the same path -> checkpoint survived on disk
        async with CheckpointManager(path) as manager2:
            cfg = {"configurable": {"thread_id": session_id}}
            cp = await manager2.require_saver().aget(cfg)
            assert cp is not None
            assert cp["channel_values"]["value"] == 2

    _run(scenario())


def test_delete_thread_persists_across_reopen(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "checkpoints.sqlite"
        session_id = "ses_test"
        async with CheckpointManager(path) as manager:
            graph = _build_graph(manager.require_saver())
            cfg = {"configurable": {"thread_id": session_id}}
            await graph.ainvoke({"value": 1}, cfg)
            await manager.delete_thread(session_id)
            assert await manager.require_saver().aget(cfg) is None
        async with CheckpointManager(path) as manager2:
            cfg = {"configurable": {"thread_id": session_id}}
            assert await manager2.require_saver().aget(cfg) is None

    _run(scenario())


def test_session_isolation_and_independent_delete(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "checkpoints.sqlite"
        cfg_a = {"configurable": {"thread_id": "ses_a"}}
        cfg_b = {"configurable": {"thread_id": "ses_b"}}
        async with CheckpointManager(path) as manager:
            saver = manager.require_saver()
            graph = _build_graph(saver)
            await graph.ainvoke({"value": 1}, cfg_a)
            await graph.ainvoke({"value": 10}, cfg_b)
            assert (await saver.aget(cfg_a))["channel_values"]["value"] == 2
            assert (await saver.aget(cfg_b))["channel_values"]["value"] == 11
            await manager.delete_thread("ses_a")
            assert await saver.aget(cfg_a) is None
            assert await saver.aget(cfg_b) is not None
        async with CheckpointManager(path) as manager2:
            saver2 = manager2.require_saver()
            assert await saver2.aget(cfg_a) is None
            assert (await saver2.aget(cfg_b))["channel_values"]["value"] == 11

    _run(scenario())


def test_checkpoint_db_separate_from_app_db(tmp_path: Path) -> None:
    async def scenario() -> None:
        cp_path = tmp_path / "checkpoints.sqlite"
        app_path = tmp_path / "app.sqlite"
        app_db = await RuntimeDatabase.open(app_path)
        try:
            async with CheckpointManager(cp_path) as manager:
                graph = _build_graph(manager.require_saver())
                await graph.ainvoke({"value": 1}, {"configurable": {"thread_id": "ses_x"}})
            assert cp_path.exists()
            raw = sqlite3.connect(cp_path)
            try:
                cp_tables = {
                    r[0]
                    for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            finally:
                raw.close()
            assert any("checkpoint" in t for t in cp_tables)
            assert not ({"sessions", "runs", "events", "runtime_state"} & cp_tables)
            app_tables = await app_db.table_names()
            assert {"sessions", "runs", "events", "runtime_state"} <= app_tables
            assert not (cp_tables & app_tables)
        finally:
            await app_db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# LANGGRAPH_STRICT_MSGPACK
# --------------------------------------------------------------------------

def test_strict_msgpack_set_before_saver_enter(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LANGGRAPH_STRICT_MSGPACK", raising=False)
    observed: dict = {}
    real_from_conn_string = AsyncSqliteSaver.from_conn_string

    class _ObservingContext:
        def __init__(self, inner) -> None:
            self._inner = inner

        async def __aenter__(self):
            observed["at_enter"] = os.environ.get("LANGGRAPH_STRICT_MSGPACK")
            return await self._inner.__aenter__()

        async def __aexit__(self, *a):
            return await self._inner.__aexit__(*a)

    monkeypatch.setattr(
        AsyncSqliteSaver,
        "from_conn_string",
        staticmethod(lambda p: _ObservingContext(real_from_conn_string(p))),
    )

    async def scenario() -> None:
        async with CheckpointManager(tmp_path / "checkpoints.sqlite"):
            pass

    _run(scenario())
    assert observed["at_enter"] == "true"


def test_strict_msgpack_does_not_override_explicit_value(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "false")

    async def scenario() -> None:
        async with CheckpointManager(tmp_path / "checkpoints.sqlite"):
            assert os.environ["LANGGRAPH_STRICT_MSGPACK"] == "false"

    _run(scenario())
    assert os.environ["LANGGRAPH_STRICT_MSGPACK"] == "false"


# --------------------------------------------------------------------------
# require_saver / closed-state behavior
# --------------------------------------------------------------------------

def test_require_saver_before_open_raises(tmp_path: Path) -> None:
    manager = CheckpointManager(tmp_path / "checkpoints.sqlite")
    with pytest.raises(RuntimeError):
        manager.require_saver()


def test_require_saver_after_close_raises(tmp_path: Path) -> None:
    manager = CheckpointManager(tmp_path / "checkpoints.sqlite")

    async def scenario() -> None:
        async with manager:
            pass

    _run(scenario())
    with pytest.raises(RuntimeError):
        manager.require_saver()


def test_delete_thread_when_closed_raises(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = CheckpointManager(tmp_path / "checkpoints.sqlite")
        with pytest.raises(RuntimeError):
            await manager.delete_thread("ses_test")

    _run(scenario())


# --------------------------------------------------------------------------
# lifecycle: normal exit, exception, BaseException, cancellation, setup failure
# --------------------------------------------------------------------------

def test_normal_exit_resets_state(monkeypatch, tmp_path: Path) -> None:
    log: list = []
    saver = _FakeSaver()
    _patch_from_conn_string(monkeypatch, _FakeContext(saver, log))
    manager = CheckpointManager(tmp_path / "checkpoints.sqlite")

    async def scenario() -> None:
        async with manager:
            assert manager.saver is saver
            assert manager.require_saver() is saver

    _run(scenario())
    assert manager.saver is None
    assert manager._context is None
    assert log == ["enter", ("exit", None)]
    with pytest.raises(RuntimeError):
        manager.require_saver()


def test_body_exception_propagates_and_resets(monkeypatch, tmp_path: Path) -> None:
    log: list = []
    _patch_from_conn_string(monkeypatch, _FakeContext(_FakeSaver(), log))
    manager = CheckpointManager(tmp_path / "checkpoints.sqlite")

    async def scenario() -> None:
        async with manager:
            raise ValueError("body boom")

    with pytest.raises(ValueError, match="body"):
        _run(scenario())
    assert manager.saver is None
    assert manager._context is None
    assert log == ["enter", ("exit", ValueError)]


class _BaseBoom(BaseException):
    pass


def test_body_base_exception_propagates_and_resets(monkeypatch, tmp_path: Path) -> None:
    log: list = []
    _patch_from_conn_string(monkeypatch, _FakeContext(_FakeSaver(), log))
    manager = CheckpointManager(tmp_path / "checkpoints.sqlite")

    async def scenario() -> None:
        async with manager:
            raise _BaseBoom("base boom")

    with pytest.raises(_BaseBoom):
        _run(scenario())
    assert manager.saver is None
    assert manager._context is None
    assert log[-1] == ("exit", _BaseBoom)


def test_cancellation_resets_state(monkeypatch, tmp_path: Path) -> None:
    log: list = []
    _patch_from_conn_string(monkeypatch, _FakeContext(_FakeSaver(), log))

    async def scenario() -> None:
        manager = CheckpointManager(tmp_path / "checkpoints.sqlite")
        entered = asyncio.Event()

        async def body() -> None:
            async with manager:
                entered.set()
                await asyncio.sleep(3600)

        task = asyncio.create_task(body())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.saver is None
        assert manager._context is None
        assert log[-1] == ("exit", asyncio.CancelledError)

    _run(scenario())


def test_setup_failure_exits_context_and_resets(monkeypatch, tmp_path: Path) -> None:
    log: list = []
    _patch_from_conn_string(monkeypatch, _FakeContext(_BoomSetupSaver(), log))
    manager = CheckpointManager(tmp_path / "checkpoints.sqlite")

    async def scenario() -> None:
        await manager.__aenter__()

    with pytest.raises(RuntimeError, match="setup boom"):
        _run(scenario())
    assert manager.saver is None
    assert manager._context is None
    assert log == ["enter", ("exit", None)]


def test_cleanup_error_does_not_mask_body_exception(monkeypatch, tmp_path: Path) -> None:
    class _NoisyContext:
        async def __aenter__(self):
            return _FakeSaver()

        async def __aexit__(self, exc_type, exc, tb):
            raise RuntimeError("cleanup boom")

    _patch_from_conn_string(monkeypatch, _NoisyContext())
    manager = CheckpointManager(tmp_path / "checkpoints.sqlite")

    async def scenario() -> None:
        async with manager:
            raise ValueError("body boom")

    with pytest.raises(ValueError, match="body"):
        _run(scenario())
    assert manager.saver is None
    assert manager._context is None


def test_cleanup_error_propagates_on_normal_exit(monkeypatch, tmp_path: Path) -> None:
    class _NoisyContext:
        async def __aenter__(self):
            return _FakeSaver()

        async def __aexit__(self, exc_type, exc, tb):
            raise RuntimeError("cleanup boom")

    _patch_from_conn_string(monkeypatch, _NoisyContext())
    manager = CheckpointManager(tmp_path / "checkpoints.sqlite")

    async def scenario() -> None:
        async with manager:
            pass

    with pytest.raises(RuntimeError, match="cleanup boom"):
        _run(scenario())
    assert manager.saver is None
    assert manager._context is None


# --------------------------------------------------------------------------
# re-entry semantics
# --------------------------------------------------------------------------

def test_reentry_while_open_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = CheckpointManager(tmp_path / "checkpoints.sqlite")
        await manager.__aenter__()
        try:
            with pytest.raises(RuntimeError):
                await manager.__aenter__()
        finally:
            await manager.__aexit__(None, None, None)
        assert manager.saver is None

    _run(scenario())


def test_can_reenter_after_close(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = CheckpointManager(tmp_path / "checkpoints.sqlite")
        async with manager:
            assert manager.saver is not None
        assert manager.saver is None
        async with manager:
            assert manager.saver is not None
        assert manager.saver is None

    _run(scenario())
