"""Task 13: process-level Runtime restart end-to-end tests.

Spawns the real Runtime as an INDEPENDENT Python process — the test-only fixture
in ``runtime_process_fixture.py`` — using a deterministic fake RunnerFactory (no
live LLM, no Houdini). A real loopback WebSocket carries every command and event.
These tests verify durable session/run/event persistence and exact no-loss /
no-duplicate replay across a hard restart, plus interrupted-run reconciliation
(``runtime.interrupted``) and its idempotence. B2b extends the same fixture and
public socket path with a deterministic Workspace provider to verify trusted
Workspace lifecycle persistence, replay, CAS, isolation, and failure atomicity.

The fixture is spawned with ``python -m tests.runtime.runtime_process_fixture``;
no pytest in-process fake server substitutes for the real socket here.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from eee_agent.changesets import OwnedNodeRef, WorkspaceManifest
from eee_agent.changesets.repository import ChangeSetRepository
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.protocol import PROTOCOL, encode_envelope

# Repo root owns the importable ``tests`` package (for ``-m`` module resolution
# in the spawned subprocess) and the editable ``eee_agent`` install.
_WORKTREE = Path(__file__).resolve().parents[2]
_FIXTURE_MODULE = "tests.runtime.runtime_process_fixture"
_DISCOVERY_POLL_TIMEOUT = 30.0
_DISCOVERY_POLL_INTERVAL = 0.05
_WORKSPACE_CONTROL = "workspace_fixture.json"
_WORKSPACE_EVENT_ENTERED = "workspace_event_entered"
_CHANGESET_EFFECT = "changeset_effect.json"
_CHANGESET_EFFECT_MARKER = "changeset_effect_committed"


def _cmd(request_id: str, type_: str, payload: dict) -> dict:
    return {
        "protocol": PROTOCOL,
        "kind": "command",
        "request_id": request_id,
        "type": type_,
        "payload": payload,
    }


class _FixtureProcess:
    """A spawned Runtime fixture process with stderr drained to a buffer.

    Readiness is signaled by the discovery file appearing on disk (no stdout
    protocol). The parent hard-kills the worker so a half-finished run is left
    non-terminal for reconciliation coverage and the OS releases the exclusive
    lock deterministically.

    Implementation note: under a uv-managed venv on Windows,
    ``sys.executable`` (``.venv/Scripts/python.exe``) is a *launcher* that
    re-execs the real interpreter as a child, so ``Popen.pid`` is the launcher
    while ``os.getpid()`` in the fixture (recorded in discovery) is the worker.
    Therefore readiness matches on a freshly-written discovery (stale files are
    cleared first), and shutdown kills the worker PID from discovery in addition
    to the spawned process.
    """

    def __init__(self, home: Path, *, mode: str = "complete") -> None:
        self.home = home
        self.mode = mode
        self.proc: subprocess.Popen | None = None
        self._worker_pid: int | None = None
        self._stderr_lines: list[str] = []
        self._drainer: threading.Thread | None = None

    def start(self) -> None:
        # Clear any stale discovery/token so readiness polling only observes a
        # file written by THIS process (the prior worker was killed in stop()).
        self._clear_stale_discovery()
        env = dict(os.environ)
        env["EEE_RUNTIME_HOME"] = str(self.home)
        kwargs: dict = dict(
            cwd=str(_WORKTREE),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if os.name != "nt":
            # Own session/group so stop() can kill the whole tree on POSIX.
            kwargs["start_new_session"] = True
        self.proc = subprocess.Popen(
            [sys.executable, "-m", _FIXTURE_MODULE, self.mode],
            **kwargs,
        )
        self._drainer = threading.Thread(target=self._drain_stderr, daemon=True)
        self._drainer.start()

    def _clear_stale_discovery(self) -> None:
        for name in ("runtime.json", "runtime.token"):
            try:
                (self.home / "state" / name).unlink()
            except FileNotFoundError:
                pass

    def _drain_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr_lines.append(line)

    @property
    def discovery_path(self) -> Path:
        return self.home / "state" / "runtime.json"

    def wait_for_discovery(self) -> dict:
        deadline = time.monotonic() + _DISCOVERY_POLL_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                break  # exited before ready
            if self.discovery_path.exists():
                data = json.loads(self.discovery_path.read_text(encoding="utf-8"))
                self._worker_pid = data.get("pid")
                return data
            time.sleep(_DISCOVERY_POLL_INTERVAL)
        raise AssertionError(
            "fixture did not publish discovery within "
            f"{_DISCOVERY_POLL_TIMEOUT}s; stderr:\n" + self.stderr_text
        )

    def read_token(self) -> str:
        return (self.home / "state" / "runtime.token").read_text(encoding="utf-8")

    def stop(self) -> None:
        # Kill the real worker (from discovery) AND the spawned launcher/leader
        # so no process survives holding the lock, port, or SQLite files.
        pids: list[int] = []
        if self._worker_pid is not None:
            pids.append(self._worker_pid)
        if self.proc is not None:
            pids.append(self.proc.pid)
        for pid in dict.fromkeys(pids):
            self._kill_tree(pid)
        if self.proc is not None:
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        # Give the OS a moment to release the byte-range lock before a successor
        # fixture tries to acquire it.
        time.sleep(0.3)

    @staticmethod
    def _kill_tree(pid: int) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            import signal as _signal

            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.kill(pid, _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    @property
    def stderr_text(self) -> str:
        return "".join(self._stderr_lines)


@pytest.fixture
def runtime_home(tmp_path: Path) -> Path:
    return tmp_path / "home"


# --------------------------------------------------------------------------
# WebSocket client helpers (real ``websockets`` client)
# --------------------------------------------------------------------------


async def _connect(discovery: dict, token: str):
    return await connect(
        f"ws://{discovery['host']}:{discovery['port']}",
        additional_headers={"Authorization": f"Bearer {token}"},
        compression=None,
    )


async def _recv_envelope(ws, *, timeout: float = 5.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


async def _request(ws, request_id: str, type_: str, payload: dict) -> dict:
    """Send a command and read exactly its response (events stay queued).

    Safe only when no subscription is streaming interleaved events; otherwise
    use ``_dispatch``.
    """
    await ws.send(encode_envelope(_cmd(request_id, type_, payload)))
    while True:
        env = await _recv_envelope(ws)
        if env.get("kind") == "response" and env.get("request_id") == request_id:
            return env


async def _dispatch_until(
    ws,
    *,
    want_responses: set[str],
    stop_on_event: tuple[str, object] | None = None,
) -> tuple[dict[str, dict], list[dict]]:
    """Read envelopes, collecting responses by request_id and events in order.

    Returns when ``stop_on_event`` (an event ``type`` whose payload ``to`` equals
    the given value) is seen, or when every ``want_responses`` id has arrived
    and no stop predicate is set. Events are returned in wire order.
    """
    responses: dict[str, dict] = {}
    events: list[dict] = []
    while True:
        env = await _recv_envelope(ws)
        if env.get("kind") == "response":
            responses[env["request_id"]] = env
            if stop_on_event is None and want_responses <= responses.keys():
                return responses, events
            continue
        if env.get("kind") == "event":
            events.append(env)
            if stop_on_event is not None:
                etype, target = stop_on_event
                if env.get("type") == etype and env["payload"].get("to") == target:
                    return responses, events


async def _dispatch_for_response_and_event(
    ws, request_id: str, event_type: str
) -> tuple[dict, dict]:
    response = None
    event = None
    while response is None or event is None:
        env = await _recv_envelope(ws)
        if (
            env.get("kind") == "response"
            and env.get("request_id") == request_id
        ):
            response = env
        elif env.get("kind") == "event" and env.get("type") == event_type:
            event = env
    return response, event


def _write_workspace_control(home: Path, data: dict) -> None:
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    destination = state_dir / _WORKSPACE_CONTROL
    encoded = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(
        dir=str(state_dir),
        prefix=f"{_WORKSPACE_CONTROL}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _remove_workspace_control(home: Path) -> None:
    try:
        (home / "state" / _WORKSPACE_CONTROL).unlink()
    except FileNotFoundError:
        pass


def _wait_for_file(path: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"fixture file did not appear: {path.name}")


def test_workspace_control_publication_uses_atomic_replace(
    runtime_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destinations: list[Path] = []
    real_replace = os.replace

    def spy_replace(src, dst):  # type: ignore[no-untyped-def]
        destinations.append(Path(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    _write_workspace_control(runtime_home, {"manifest_status": "healthy"})

    expected = runtime_home / "state" / _WORKSPACE_CONTROL
    assert destinations == [expected]
    assert json.loads(expected.read_text(encoding="utf-8")) == {
        "manifest_status": "healthy"
    }


def test_houdini_smoke_required_fingerprint_reads_fail_closed() -> None:
    from tests.runtime.changeset_houdini_smoke import _required_read

    def unreadable():
        raise RuntimeError("simulated HOM read failure")

    with pytest.raises(
        RuntimeError,
        match=r"node /obj/example parameter tx data.*RuntimeError",
    ):
        _required_read(
            "node /obj/example parameter tx data",
            unreadable,
        )


def test_houdini_smoke_force_stops_runtime_launcher_and_worker(
    runtime_home: Path,
) -> None:
    from tests.runtime.changeset_houdini_smoke import _RuntimeFixtureProcess

    from eee_agent.runtime.lock import RuntimeLock
    from eee_agent.runtime.paths import RuntimePaths

    async def scenario() -> tuple[int, int, str | None]:
        paths = RuntimePaths(
            home=runtime_home,
            state_dir=runtime_home / "state",
            app_db=runtime_home / "state" / "app.sqlite",
            checkpoints_db=runtime_home / "state" / "checkpoints.sqlite",
            lock_file=runtime_home / "state" / "runtime.lock",
            discovery_file=runtime_home / "state" / "runtime.json",
            token_file=runtime_home / "state" / "runtime.token",
        )
        paths.create_used_directories()
        process = _RuntimeFixtureProcess(
            runtime_home,
            mode="bridge_hang",
            graceful_timeout=0.1,
        )
        process.start()
        try:
            discovery, _token = await process.wait_for_discovery(paths)
            assert discovery["pid"] == process.worker_pid
            assert process.launcher_pid > 0
            assert process.worker_pid > 0
            cleanup_error = await process.stop(paths)
            return (
                process.launcher_pid,
                process.worker_pid,
                cleanup_error,
            )
        finally:
            await process.stop(paths)

    launcher_pid, worker_pid, cleanup_error = asyncio.run(scenario())
    assert cleanup_error == "Runtime fixture required forced termination"
    assert _RuntimeFixtureProcess.pid_exists(launcher_pid) is False
    assert _RuntimeFixtureProcess.pid_exists(worker_pid) is False
    assert not (runtime_home / "state" / "runtime.json").exists()
    assert not (runtime_home / "state" / "runtime.token").exists()
    with RuntimeLock(runtime_home / "state" / "runtime.lock"):
        pass


async def _wait_for_path(path: Path, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"fixture marker did not appear: {path.name}")


def _selection_control(
    *,
    workspace_id: str,
    run_id: str,
    path: str = "/obj/ws",
    scene_epoch: int = 7,
    status: str = "ok",
) -> dict:
    return {
        "selection": {
            "status": status,
            "instance_id": "hou_instance_1",
            "scene_epoch": scene_epoch,
            "observations": [
                {
                    "path": path,
                    "node_type": "geo",
                    "parent_path": "/obj",
                    "is_locked": False,
                    "workspace_id": workspace_id,
                    "node_id": "n_root",
                    "capability": "modeling",
                    "role": "root",
                    "schema_version": 1,
                    "created_by_run": run_id,
                }
            ],
        },
        "manifest_status": "healthy",
    }


async def _wait_for_completed_run(ws, session_id: str) -> str:
    started = await _request(
        ws,
        f"run-{time.monotonic_ns()}",
        "run.start",
        {"session_id": session_id, "user_input": "workspace fixture"},
    )
    assert started["ok"] is True
    run_id = started["result"]["run_id"]
    for index in range(200):
        snap = await _request(
            ws,
            f"snap-{index}-{time.monotonic_ns()}",
            "session.snapshot",
            {"session_id": session_id},
        )
        runs = {run["run_id"]: run for run in snap["result"]["runs"]}
        if runs[run_id]["status"] == "Completed":
            return run_id
        await asyncio.sleep(0.01)
    raise AssertionError("fixture run did not complete")


def _seed_inactive_workspace(
    home: Path,
    *,
    session_id: str,
    run_id: str,
    workspace_id: str,
    node_id: str,
    path: str,
    scene_epoch: int = 7,
) -> str:
    async def seed() -> str:
        database = await RuntimeDatabase.open(home / "state" / "app.sqlite")
        try:
            repository = ChangeSetRepository(database)
            node = OwnedNodeRef(
                node_id=node_id,
                path=path,
                node_type="geo",
                parent_path="/obj",
                capability="modeling",
                role="root",
            )
            manifest = WorkspaceManifest.build(
                workspace_id=workspace_id,
                session_id=session_id,
                instance_id="hou_instance_1",
                scene_epoch=scene_epoch,
                roots=(node,),
                nodes=(node,),
                created_by_run=run_id,
                updated_at=datetime.now(timezone.utc),
            )
            return (await repository.insert_workspace(manifest)).revision
        finally:
            await database.close()

    return asyncio.run(seed())


# --------------------------------------------------------------------------
# 1. restart replay: no loss, no duplicate, monotonic sequences
# --------------------------------------------------------------------------


def test_restart_replays_all_events_without_loss_or_duplicate(
    runtime_home: Path,
) -> None:
    fixture = _FixtureProcess(runtime_home, mode="complete")
    fixture.start()
    try:
        discovery = fixture.wait_for_discovery()
        token = fixture.read_token()

        async def phase_one() -> tuple[str, list[tuple[int, str]]]:
            async with await _connect(discovery, token) as ws:
                created = await _request(ws, "c1", "session.create", {"title": "Restart"})
                assert created["ok"] is True
                session_id = created["result"]["session_id"]
                # Subscribe BEFORE start_run so every event is captured live.
                await ws.send(encode_envelope(
                    _cmd("s1", "session.subscribe", {"session_id": session_id, "last_seq": 0})
                ))
                await ws.send(encode_envelope(
                    _cmd("r1", "run.start", {"session_id": session_id, "user_input": "hello"})
                ))
                responses, events = await _dispatch_until(
                    ws,
                    want_responses={"s1", "r1"},
                    stop_on_event=("run.state_changed", "Completed"),
                )
                assert responses["s1"]["ok"] is True
                assert responses["r1"]["ok"] is True
                live = [(e["seq"], e["type"]) for e in events if e.get("seq") is not None]
                return session_id, live

        session_id, live = asyncio.run(phase_one())
    finally:
        fixture.stop()

    assert live, "no live events were captured"
    recorded_seqs = [seq for seq, _ in live]
    last_seq = max(recorded_seqs)
    # Phase 1 sequences are contiguous from 1 (replay of session.created + live).
    assert recorded_seqs == list(range(1, last_seq + 1))

    # --- Phase 2: restart over the SAME home, reconnect, verify persistence. ---
    fixture2 = _FixtureProcess(runtime_home, mode="complete")
    fixture2.start()
    try:
        discovery2 = fixture2.wait_for_discovery()
        token2 = fixture2.read_token()

        async def phase_two() -> None:
            async with await _connect(discovery2, token2) as ws:
                listed = await _request(ws, "l1", "session.list", {})
                assert listed["ok"] is True
                assert any(
                    s["session_id"] == session_id
                    for s in listed["result"]["sessions"]
                )
                # Full replay must equal the phase-1 live sequence exactly.
                replay = await _request(ws, "rp1", "events.replay", {
                    "session_id": session_id, "after_seq": 0, "limit": 1000,
                })
                assert replay["ok"] is True
                # Replay result events use EventRecord.to_dict() (key: event_type);
                # phase-1 wire envelopes used `type` with the same value.
                replayed = [
                    (e["seq"], e["event_type"]) for e in replay["result"]["events"]
                ]
                assert replayed == live  # no loss, no duplicate, same order
                seqs = [seq for seq, _ in replayed]
                assert seqs == sorted(seqs)  # strictly monotonic
                assert len(seqs) == len(set(seqs))  # no duplicate seqs
                # Subscribing from the recorded boundary redelivers nothing.
                await ws.send(encode_envelope(
                    _cmd("s2", "session.subscribe",
                         {"session_id": session_id, "last_seq": last_seq})
                ))
                sub_resp = await _recv_envelope(ws)
                assert sub_resp["ok"] is True
                assert sub_resp["result"]["last_seq"] == last_seq
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), timeout=0.5)

        asyncio.run(phase_two())
    finally:
        fixture2.stop()


# --------------------------------------------------------------------------
# 2. interrupted-run reconciliation: Failed/runtime.interrupted, idempotent
# --------------------------------------------------------------------------


def test_restart_reconciles_interrupted_run_as_failed(runtime_home: Path) -> None:
    # --- Phase A: blocking runner; leave a non-terminal run, then hard-kill. ---
    fixture = _FixtureProcess(runtime_home, mode="block")
    fixture.start()
    state: dict[str, object] = {}
    try:
        discovery = fixture.wait_for_discovery()
        token = fixture.read_token()

        async def phase_a() -> None:
            async with await _connect(discovery, token) as ws:
                created = await _request(ws, "c1", "session.create", {"title": "Interrupt"})
                assert created["ok"] is True
                session_id = created["result"]["session_id"]
                state["session_id"] = session_id
                await ws.send(encode_envelope(
                    _cmd("s1", "session.subscribe", {"session_id": session_id, "last_seq": 0})
                ))
                await ws.send(encode_envelope(
                    _cmd("r1", "run.start", {"session_id": session_id, "user_input": "x"})
                ))
                responses, _ = await _dispatch_until(
                    ws,
                    want_responses={"s1", "r1"},
                    stop_on_event=("run.state_changed", "Planning"),
                )
                assert responses["s1"]["ok"] is True
                assert responses["r1"]["ok"] is True
                state["run_id"] = responses["r1"]["result"]["run_id"]

        asyncio.run(phase_a())
    finally:
        fixture.stop()  # hard kill -> run stays non-terminal (Planning)

    session_id = state["session_id"]
    run_id = state["run_id"]

    # --- Phase B: restart reconciles the interrupted run to Failed. ---
    fixture2 = _FixtureProcess(runtime_home, mode="complete")
    fixture2.start()
    last_seq_b: int
    try:
        discovery2 = fixture2.wait_for_discovery()
        token2 = fixture2.read_token()

        async def phase_b() -> int:
            async with await _connect(discovery2, token2) as ws:
                snap = await _request(ws, "sn1", "session.snapshot",
                                      {"session_id": session_id})
                assert snap["ok"] is True
                # active_run_id cleared by reconciliation.
                assert snap["result"]["active_run"] is None
                runs = {r["run_id"]: r for r in snap["result"]["runs"]}
                assert run_id in runs
                assert runs[run_id]["status"] == "Failed"
                assert runs[run_id]["failure_json"]["code"] == "runtime.interrupted"
                # Reconciliation appended the terminal transition + run.failed.
                replay = await _request(ws, "rp1", "events.replay", {
                    "session_id": session_id, "after_seq": 0, "limit": 1000,
                })
                events = replay["result"]["events"]
                assert events[-1]["event_type"] == "run.failed"
                assert events[-2]["event_type"] == "run.state_changed"
                assert events[-2]["payload"]["to"] == "Failed"
                assert events[-2]["payload"].get("reason") == "interrupted"
                failed = [e for e in events if e["event_type"] == "run.failed"]
                assert failed[-1]["payload"]["error"]["code"] == "runtime.interrupted"
                return replay["result"]["last_seq"]

        last_seq_b = asyncio.run(phase_b())
    finally:
        fixture2.stop()

    # --- Phase C: restart AGAIN; reconciliation is idempotent. ---
    fixture3 = _FixtureProcess(runtime_home, mode="complete")
    fixture3.start()
    try:
        discovery3 = fixture3.wait_for_discovery()
        token3 = fixture3.read_token()

        async def phase_c() -> None:
            async with await _connect(discovery3, token3) as ws:
                replay = await _request(ws, "rp2", "events.replay", {
                    "session_id": session_id, "after_seq": 0, "limit": 1000,
                })
                # No new reconciliation events: last_seq unchanged.
                assert replay["result"]["last_seq"] == last_seq_b
                snap = await _request(ws, "sn2", "session.snapshot",
                                      {"session_id": session_id})
                assert snap["result"]["active_run"] is None
                runs = {r["run_id"]: r for r in snap["result"]["runs"]}
                assert runs[run_id]["status"] == "Failed"
                assert runs[run_id]["failure_json"]["code"] == "runtime.interrupted"

        asyncio.run(phase_c())
    finally:
        fixture3.stop()


# --------------------------------------------------------------------------
# 3. ChangeSet crash after Bridge effect, receipt-only restart recovery
# --------------------------------------------------------------------------


def test_restart_recovers_changeset_receipt_without_duplicate_apply(
    runtime_home: Path,
) -> None:
    crash = _FixtureProcess(runtime_home, mode="changeset_crash")
    crash.start()
    marker = runtime_home / "state" / _CHANGESET_EFFECT_MARKER
    try:
        _wait_for_file(marker)
        assert crash.proc is not None
        crash.proc.wait(timeout=10)
    finally:
        crash.stop()

    db_path = runtime_home / "state" / "app.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        applying = conn.execute(
            "SELECT change_id, state FROM changesets"
        ).fetchone()
        assert applying is not None
        change_id = applying["change_id"]
        assert applying["state"] == "Applying"
        approval = conn.execute(
            "SELECT decision FROM approvals WHERE change_id = ?", (change_id,)
        ).fetchone()
        assert approval["decision"] == "Consumed"
        assert conn.execute(
            "SELECT COUNT(*) FROM change_receipts WHERE change_id = ?", (change_id,)
        ).fetchone()[0] == 0

    recovery = _FixtureProcess(runtime_home, mode="changeset_recover")
    recovery.start()
    try:
        recovery.wait_for_discovery()
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            stored = conn.execute(
                "SELECT state FROM changesets WHERE change_id = ?", (change_id,)
            ).fetchone()
            assert stored["state"] == "Applied"
            receipt = conn.execute(
                "SELECT status FROM change_receipts WHERE change_id = ?",
                (change_id,),
            ).fetchone()
            assert receipt["status"] == "Applied"
            events = conn.execute(
                "SELECT event_type FROM events WHERE event_type IN "
                "('changeset.applied', 'recovery.critical') ORDER BY seq"
            ).fetchall()
            assert [row["event_type"] for row in events] == ["changeset.applied"]
    finally:
        recovery.stop()

    evidence = json.loads(
        (runtime_home / "state" / _CHANGESET_EFFECT).read_text(encoding="utf-8")
    )
    assert evidence["apply_count"] == 1
    assert evidence["receipt"]["change_id"] == change_id


# --------------------------------------------------------------------------
# 4. trusted Workspace lifecycle through the public process boundary
# --------------------------------------------------------------------------


def test_workspace_public_create_bind_replay_restart_and_offline_inspect(
    runtime_home: Path,
) -> None:
    workspace_id = f"ws_{'a' * 32}"
    fixture = _FixtureProcess(runtime_home, mode="workspace")
    fixture.start()
    state: dict[str, object] = {}
    try:
        discovery = fixture.wait_for_discovery()
        token = fixture.read_token()

        async def phase_one() -> None:
            async with await _connect(discovery, token) as ws:
                created_session = await _request(
                    ws, "c-workspace", "session.create", {"title": "Workspace"}
                )
                session_id = created_session["result"]["session_id"]
                state["session_id"] = session_id
                run_id = await _wait_for_completed_run(ws, session_id)
                _write_workspace_control(
                    runtime_home,
                    _selection_control(
                        workspace_id=workspace_id, run_id=run_id
                    ),
                )
                replay = await _request(
                    ws,
                    "before-workspace",
                    "events.replay",
                    {"session_id": session_id, "after_seq": 0, "limit": 1000},
                )
                boundary = replay["result"]["last_seq"]
                subscribed = await _request(
                    ws,
                    "sub-workspace",
                    "session.subscribe",
                    {"session_id": session_id, "last_seq": boundary},
                )
                assert subscribed["ok"] is True

                await ws.send(
                    encode_envelope(
                        _cmd(
                            "workspace-create",
                            "workspace.create",
                            {
                                "session_id": session_id,
                                "expected_scene_epoch": 7,
                            },
                        )
                    )
                )
                create_response, create_event = (
                    await _dispatch_for_response_and_event(
                        ws, "workspace-create", "workspace.created"
                    )
                )
                assert create_response["ok"] is True
                assert create_response["result"]["changed"] is True
                assert create_event["payload"]["workspace_id"] == workspace_id
                state["create_seq"] = create_event["seq"]
                state["created_revision"] = create_response["result"]["workspace"][
                    "revision"
                ]

                idempotent = await _request(
                    ws,
                    "workspace-create-noop",
                    "workspace.create",
                    {
                        "session_id": session_id,
                        "expected_scene_epoch": 7,
                    },
                )
                assert idempotent["ok"] is True
                assert idempotent["result"]["changed"] is False
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), timeout=0.3)

                _write_workspace_control(
                    runtime_home,
                    _selection_control(
                        workspace_id=workspace_id,
                        run_id=run_id,
                        path="/obj/renamed",
                        scene_epoch=8,
                    ),
                )
                await ws.send(
                    encode_envelope(
                        _cmd(
                            "workspace-bind",
                            "workspace.bind",
                            {
                                "session_id": session_id,
                                "workspace_id": workspace_id,
                                "expected_manifest_revision": state[
                                    "created_revision"
                                ],
                                "expected_scene_epoch": 8,
                            },
                        )
                    )
                )
                bind_response, bind_event = (
                    await _dispatch_for_response_and_event(
                        ws, "workspace-bind", "workspace.bound"
                    )
                )
                assert bind_response["ok"] is True
                assert bind_response["result"]["changed"] is True
                assert bind_response["result"]["workspace"]["scene_epoch"] == 8
                assert bind_event["seq"] == create_event["seq"] + 1
                state["bound_revision"] = bind_response["result"]["workspace"][
                    "revision"
                ]
                state["bind_seq"] = bind_event["seq"]

                inspected = await _request(
                    ws,
                    "workspace-inspect",
                    "workspace.inspect",
                    {
                        "session_id": session_id,
                        "workspace_id": None,
                        "expected_scene_epoch": 8,
                    },
                )
                assert inspected["ok"] is True
                assert inspected["result"]["status"] == "Healthy"
                assert inspected["result"]["active_workspace_id"] == workspace_id
                assert inspected["result"]["target_manifest"]["nodes"][0][
                    "path"
                ] == "/obj/renamed"

                snapshot = await _request(
                    ws,
                    "workspace-snapshot",
                    "session.snapshot",
                    {"session_id": session_id},
                )
                assert set(snapshot["result"]) == {
                    "session",
                    "runs",
                    "active_run",
                    "snapshot_seq",
                    "has_earlier_runs",
                    "earliest_included_run_id",
                    "version_report",
                }

        asyncio.run(phase_one())
    finally:
        fixture.stop()

    _remove_workspace_control(runtime_home)
    fixture2 = _FixtureProcess(runtime_home, mode="workspace")
    fixture2.start()
    try:
        discovery2 = fixture2.wait_for_discovery()
        token2 = fixture2.read_token()

        async def phase_two() -> None:
            async with await _connect(discovery2, token2) as ws:
                session_id = state["session_id"]
                inspected = await _request(
                    ws,
                    "workspace-offline",
                    "workspace.inspect",
                    {
                        "session_id": session_id,
                        "workspace_id": workspace_id,
                        "expected_scene_epoch": None,
                    },
                )
                assert inspected["ok"] is True
                assert inspected["result"]["status"] == "BridgeUnavailable"
                assert inspected["result"]["active_workspace_id"] == workspace_id
                assert inspected["result"]["target_manifest"]["revision"] == state[
                    "bound_revision"
                ]

                replay = await _request(
                    ws,
                    "workspace-replay",
                    "events.replay",
                    {"session_id": session_id, "after_seq": 0, "limit": 1000},
                )
                workspace_events = [
                    event
                    for event in replay["result"]["events"]
                    if event["event_type"].startswith("workspace.")
                ]
                assert [
                    (event["seq"], event["event_type"])
                    for event in workspace_events
                ] == [
                    (state["create_seq"], "workspace.created"),
                    (state["bind_seq"], "workspace.bound"),
                ]

        asyncio.run(phase_two())
    finally:
        fixture2.stop()


def test_workspace_provider_commit_failure_and_session_isolation(
    runtime_home: Path,
) -> None:
    workspace_id = f"ws_{'b' * 32}"
    fixture = _FixtureProcess(runtime_home, mode="workspace")
    fixture.start()
    try:
        discovery = fixture.wait_for_discovery()
        token = fixture.read_token()

        async def scenario() -> None:
            async with await _connect(discovery, token) as ws:
                first = await _request(
                    ws, "session-one", "session.create", {"title": "One"}
                )
                second = await _request(
                    ws, "session-two", "session.create", {"title": "Two"}
                )
                sid_one = first["result"]["session_id"]
                sid_two = second["result"]["session_id"]
                run_one = await _wait_for_completed_run(ws, sid_one)
                run_two = await _wait_for_completed_run(ws, sid_two)

                offline = _selection_control(
                    workspace_id=workspace_id,
                    run_id=run_one,
                    status="offline",
                )
                _write_workspace_control(runtime_home, offline)
                unavailable = await _request(
                    ws,
                    "workspace-provider-fail",
                    "workspace.create",
                    {
                        "session_id": sid_one,
                        "expected_scene_epoch": None,
                    },
                )
                assert unavailable["ok"] is False
                assert unavailable["error"]["code"] == (
                    "bridge.capability_unavailable"
                )

                failing = _selection_control(
                    workspace_id=workspace_id, run_id=run_one
                )
                failing["fail_event_type"] = "workspace.created"
                _write_workspace_control(runtime_home, failing)
                commit_failure = await _request(
                    ws,
                    "workspace-commit-fail",
                    "workspace.create",
                    {
                        "session_id": sid_one,
                        "expected_scene_epoch": 7,
                    },
                )
                assert commit_failure["ok"] is False
                assert commit_failure["error"]["code"] == (
                    "internal.runtime_failure"
                )
                assert _WORKSPACE_CONTROL not in json.dumps(commit_failure)

                replay_after_fail = await _request(
                    ws,
                    "workspace-after-fail",
                    "events.replay",
                    {"session_id": sid_one, "after_seq": 0, "limit": 1000},
                )
                assert not any(
                    event["event_type"].startswith("workspace.")
                    for event in replay_after_fail["result"]["events"]
                )
                missing_after_fail = await _request(
                    ws,
                    "workspace-missing-after-fail",
                    "workspace.inspect",
                    {
                        "session_id": sid_one,
                        "workspace_id": workspace_id,
                        "expected_scene_epoch": None,
                    },
                )
                assert missing_after_fail["ok"] is False
                assert missing_after_fail["error"]["code"] == (
                    "workspace.not_found"
                )

                healthy = _selection_control(
                    workspace_id=workspace_id, run_id=run_one
                )
                _write_workspace_control(runtime_home, healthy)
                created = await _request(
                    ws,
                    "workspace-retry",
                    "workspace.create",
                    {
                        "session_id": sid_one,
                        "expected_scene_epoch": 7,
                    },
                )
                assert created["ok"] is True
                assert created["result"]["changed"] is True

                isolated = await _request(
                    ws,
                    "workspace-other-session-inspect",
                    "workspace.inspect",
                    {
                        "session_id": sid_two,
                        "workspace_id": workspace_id,
                        "expected_scene_epoch": None,
                    },
                )
                assert isolated["ok"] is False
                assert isolated["error"]["code"] == "workspace.not_found"

                conflicting = _selection_control(
                    workspace_id=workspace_id, run_id=run_two
                )
                _write_workspace_control(runtime_home, conflicting)
                cross_session = await _request(
                    ws,
                    "workspace-other-session-create",
                    "workspace.create",
                    {
                        "session_id": sid_two,
                        "expected_scene_epoch": 7,
                    },
                )
                assert cross_session["ok"] is False
                assert cross_session["error"]["code"] == (
                    "workspace.session_mismatch"
                )
                replay_two = await _request(
                    ws,
                    "workspace-other-session-replay",
                    "events.replay",
                    {"session_id": sid_two, "after_seq": 0, "limit": 1000},
                )
                assert not any(
                    event["event_type"].startswith("workspace.")
                    for event in replay_two["result"]["events"]
                )

        asyncio.run(scenario())
    finally:
        fixture.stop()


def test_workspace_public_switch_concurrent_cas_noop_and_stale_preservation(
    runtime_home: Path,
) -> None:
    first_workspace = f"ws_{'c' * 32}"
    second_workspace = f"ws_{'d' * 32}"
    fixture = _FixtureProcess(runtime_home, mode="workspace")
    fixture.start()
    state: dict[str, str] = {}
    try:
        discovery = fixture.wait_for_discovery()
        token = fixture.read_token()

        async def create_first() -> None:
            async with await _connect(discovery, token) as ws:
                session = await _request(
                    ws, "switch-session", "session.create", {"title": "Switch"}
                )
                session_id = session["result"]["session_id"]
                run_id = await _wait_for_completed_run(ws, session_id)
                _write_workspace_control(
                    runtime_home,
                    _selection_control(
                        workspace_id=first_workspace, run_id=run_id
                    ),
                )
                created = await _request(
                    ws,
                    "switch-create",
                    "workspace.create",
                    {
                        "session_id": session_id,
                        "expected_scene_epoch": 7,
                    },
                )
                assert created["ok"] is True
                state["session_id"] = session_id
                state["run_id"] = run_id

        asyncio.run(create_first())
    finally:
        fixture.stop()

    _seed_inactive_workspace(
        runtime_home,
        session_id=state["session_id"],
        run_id=state["run_id"],
        workspace_id=second_workspace,
        node_id="n_second",
        path="/obj/second",
    )
    control = _selection_control(
        workspace_id=first_workspace, run_id=state["run_id"]
    )
    control["manifest_status"] = "healthy"
    _write_workspace_control(runtime_home, control)

    fixture2 = _FixtureProcess(runtime_home, mode="workspace")
    fixture2.start()
    try:
        discovery2 = fixture2.wait_for_discovery()
        token2 = fixture2.read_token()

        async def switch_concurrently() -> None:
            async with await _connect(discovery2, token2) as first_ws:
                async with await _connect(discovery2, token2) as second_ws:
                    before = await _request(
                        first_ws,
                        "switch-before",
                        "events.replay",
                        {
                            "session_id": state["session_id"],
                            "after_seq": 0,
                            "limit": 1000,
                        },
                    )
                    boundary = before["result"]["last_seq"]
                    payload = {
                        "session_id": state["session_id"],
                        "workspace_id": second_workspace,
                        "expected_active_workspace_id": first_workspace,
                        "expected_scene_epoch": 7,
                    }
                    responses = await asyncio.gather(
                        _request(
                            first_ws,
                            "switch-race-one",
                            "workspace.switch",
                            payload,
                        ),
                        _request(
                            second_ws,
                            "switch-race-two",
                            "workspace.switch",
                            payload,
                        ),
                    )
                    successes = [response for response in responses if response["ok"]]
                    failures = [response for response in responses if not response["ok"]]
                    assert len(successes) == 1
                    assert successes[0]["result"]["changed"] is True
                    assert successes[0]["result"]["active_workspace_id"] == (
                        second_workspace
                    )
                    switched_state_revision = successes[0]["result"][
                        "state_revision"
                    ]
                    assert len(failures) == 1
                    assert failures[0]["error"]["code"] == (
                        "workspace.active_conflict"
                    )

                    replay = await _request(
                        first_ws,
                        "switch-after-race",
                        "events.replay",
                        {
                            "session_id": state["session_id"],
                            "after_seq": boundary,
                            "limit": 100,
                        },
                    )
                    updated = [
                        event
                        for event in replay["result"]["events"]
                        if event["event_type"] == "workspace.updated"
                    ]
                    assert len(updated) == 1
                    assert updated[0]["payload"]["workspace_id"] == (
                        second_workspace
                    )

                    noop = await _request(
                        first_ws,
                        "switch-noop",
                        "workspace.switch",
                        {
                            "session_id": state["session_id"],
                            "workspace_id": second_workspace,
                            "expected_active_workspace_id": second_workspace,
                            "expected_scene_epoch": 7,
                        },
                    )
                    assert noop["ok"] is True
                    assert noop["result"]["changed"] is False
                    assert (
                        noop["result"]["state_revision"]
                        == switched_state_revision
                    )
                    after_noop = await _request(
                        first_ws,
                        "switch-after-noop",
                        "events.replay",
                        {
                            "session_id": state["session_id"],
                            "after_seq": updated[0]["seq"],
                            "limit": 100,
                        },
                    )
                    assert after_noop["result"]["events"] == []

                    stale_control = dict(control)
                    stale_control["manifest_status"] = "stale"
                    _write_workspace_control(runtime_home, stale_control)
                    stale = await _request(
                        first_ws,
                        "switch-stale",
                        "workspace.switch",
                        {
                            "session_id": state["session_id"],
                            "workspace_id": first_workspace,
                            "expected_active_workspace_id": second_workspace,
                            "expected_scene_epoch": 7,
                        },
                    )
                    assert stale["ok"] is False
                    assert stale["error"]["code"] == (
                        "workspace.revision_conflict"
                    )
                    inspected = await _request(
                        first_ws,
                        "switch-inspect-stale",
                        "workspace.inspect",
                        {
                            "session_id": state["session_id"],
                            "workspace_id": first_workspace,
                            "expected_scene_epoch": 7,
                        },
                    )
                    assert inspected["ok"] is True
                    assert inspected["result"]["status"] == "Stale"
                    assert inspected["result"]["active_workspace_id"] == (
                        second_workspace
                    )

                    conflict_control = dict(control)
                    conflict_control["manifest_status"] = "conflict"
                    _write_workspace_control(runtime_home, conflict_control)
                    conflict = await _request(
                        first_ws,
                        "switch-identity-conflict",
                        "workspace.switch",
                        {
                            "session_id": state["session_id"],
                            "workspace_id": first_workspace,
                            "expected_active_workspace_id": second_workspace,
                            "expected_scene_epoch": 7,
                        },
                    )
                    assert conflict["ok"] is False
                    assert conflict["error"]["code"] == (
                        "workspace.identity_conflict"
                    )

                    _write_workspace_control(runtime_home, control)
                    verified_noop = await _request(
                        first_ws,
                        "switch-verify-preserved-state",
                        "workspace.switch",
                        {
                            "session_id": state["session_id"],
                            "workspace_id": second_workspace,
                            "expected_active_workspace_id": second_workspace,
                            "expected_scene_epoch": 7,
                        },
                    )
                    assert verified_noop["ok"] is True
                    assert verified_noop["result"]["changed"] is False
                    assert verified_noop["result"]["active_workspace_id"] == (
                        second_workspace
                    )
                    assert (
                        verified_noop["result"]["state_revision"]
                        == switched_state_revision
                    )
                    after_failures = await _request(
                        first_ws,
                        "switch-after-failures",
                        "events.replay",
                        {
                            "session_id": state["session_id"],
                            "after_seq": updated[0]["seq"],
                            "limit": 100,
                        },
                    )
                    assert after_failures["result"]["events"] == []

        asyncio.run(switch_concurrently())
    finally:
        fixture2.stop()


def test_workspace_disconnect_during_commit_replays_one_complete_mutation(
    runtime_home: Path,
) -> None:
    workspace_id = f"ws_{'e' * 32}"
    marker = runtime_home / "state" / _WORKSPACE_EVENT_ENTERED
    fixture = _FixtureProcess(runtime_home, mode="workspace")
    fixture.start()
    try:
        discovery = fixture.wait_for_discovery()
        token = fixture.read_token()

        async def scenario() -> None:
            async with await _connect(discovery, token) as setup_ws:
                session = await _request(
                    setup_ws,
                    "disconnect-session",
                    "session.create",
                    {"title": "Disconnect"},
                )
                session_id = session["result"]["session_id"]
                run_id = await _wait_for_completed_run(setup_ws, session_id)

            blocked = _selection_control(
                workspace_id=workspace_id, run_id=run_id
            )
            blocked["block_event_type"] = "workspace.created"
            _write_workspace_control(runtime_home, blocked)

            ws = await _connect(discovery, token)
            try:
                await ws.send(
                    encode_envelope(
                        _cmd(
                            "disconnect-create",
                            "workspace.create",
                            {
                                "session_id": session_id,
                                "expected_scene_epoch": 7,
                            },
                        )
                    )
                )
                await _wait_for_path(marker)
            finally:
                await ws.close()

            _write_workspace_control(
                runtime_home,
                _selection_control(
                    workspace_id=workspace_id, run_id=run_id
                ),
            )

            async with await _connect(discovery, token) as replay_ws:
                workspace_events = []
                for index in range(200):
                    replay = await _request(
                        replay_ws,
                        f"disconnect-replay-{index}",
                        "events.replay",
                        {
                            "session_id": session_id,
                            "after_seq": 0,
                            "limit": 1000,
                        },
                    )
                    workspace_events = [
                        event
                        for event in replay["result"]["events"]
                        if event["event_type"].startswith("workspace.")
                    ]
                    if workspace_events:
                        break
                    await asyncio.sleep(0.01)
                assert [
                    event["event_type"] for event in workspace_events
                ] == ["workspace.created"]
                inspected = await _request(
                    replay_ws,
                    "disconnect-inspect",
                    "workspace.inspect",
                    {
                        "session_id": session_id,
                        "workspace_id": workspace_id,
                        "expected_scene_epoch": 7,
                    },
                )
                assert inspected["ok"] is True
                assert inspected["result"]["status"] == "Healthy"
                assert inspected["result"]["active_workspace_id"] == workspace_id

        asyncio.run(scenario())
    finally:
        fixture.stop()
