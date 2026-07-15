"""Task 13: process-level Runtime restart end-to-end tests.

Spawns the real Runtime as an INDEPENDENT Python process — the test-only fixture
in ``runtime_process_fixture.py`` — using a deterministic fake RunnerFactory (no
live LLM, no Houdini). A real loopback WebSocket carries every command and event.
These tests verify durable session/run/event persistence and exact no-loss /
no-duplicate replay across a hard restart, plus interrupted-run reconciliation
(``runtime.interrupted``) and its idempotence.

The fixture is spawned with ``python -m tests.runtime.runtime_process_fixture``;
no pytest in-process fake server substitutes for the real socket here.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from eee_agent.runtime.protocol import PROTOCOL, encode_envelope

# Repo root owns the importable ``tests`` package (for ``-m`` module resolution
# in the spawned subprocess) and the editable ``eee_agent`` install.
_WORKTREE = Path(__file__).resolve().parents[2]
_FIXTURE_MODULE = "tests.runtime.runtime_process_fixture"
_DISCOVERY_POLL_TIMEOUT = 30.0
_DISCOVERY_POLL_INTERVAL = 0.05


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
