"""Task 13: Runtime CLI parser and async_main lifecycle tests.

The parser is exercised in isolation (it never starts a permanent server). The
lifecycle test drives the REAL ``async_main`` in-process with an hermetic fake
provider key (no live LLM, no Houdini): it starts the Runtime, authenticates a
ping over the real loopback socket, cancels the lifecycle, and asserts every
resource — discovery files, server, service, and the exclusive lock — is
released. Tests follow the repo convention: each drives an async scenario via
``asyncio.run`` (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from eee_agent.runtime.__main__ import async_main, parse_args
from eee_agent.runtime.lock import RuntimeLock
from eee_agent.runtime.paths import RuntimePaths
from eee_agent.runtime.protocol import PROTOCOL, encode_envelope


def _cmd(request_id: str, type_: str, payload: dict) -> dict:
    return {
        "protocol": PROTOCOL,
        "kind": "command",
        "request_id": request_id,
        "type": type_,
        "payload": payload,
    }


# --------------------------------------------------------------------------
# 1. parser (no server started)
# --------------------------------------------------------------------------


def test_runtime_cli_defaults_to_loopback_and_ephemeral_port() -> None:
    args = parse_args(["serve"])
    assert args.command == "serve"
    assert args.host == "127.0.0.1"
    assert args.port == 0


def test_runtime_cli_default_graceful_timeout_is_ten() -> None:
    args = parse_args(["serve"])
    assert args.graceful_timeout == 10


def test_runtime_cli_rejects_non_loopback() -> None:
    with pytest.raises(SystemExit):
        parse_args(["serve", "--host", "0.0.0.0"])


def test_runtime_cli_rejects_loopback_variant() -> None:
    # The first implementation binds 127.0.0.1 exclusively; ::1 / localhost are
    # rejected before any socket is created.
    with pytest.raises(SystemExit):
        parse_args(["serve", "--host", "::1"])


def test_runtime_cli_rejects_unknown_positional() -> None:
    with pytest.raises(SystemExit):
        parse_args(["serve", "bogus"])


def test_runtime_cli_rejects_unknown_option() -> None:
    with pytest.raises(SystemExit):
        parse_args(["serve", "--no-such-flag"])


def test_runtime_cli_rejects_zero_graceful_timeout() -> None:
    with pytest.raises(SystemExit):
        parse_args(["serve", "--graceful-timeout", "0"])


def test_runtime_cli_rejects_negative_graceful_timeout() -> None:
    with pytest.raises(SystemExit):
        parse_args(["serve", "--graceful-timeout", "-5"])


def test_runtime_cli_rejects_non_numeric_graceful_timeout() -> None:
    with pytest.raises(SystemExit):
        parse_args(["serve", "--graceful-timeout", "soon"])


def test_runtime_cli_accepts_custom_port_and_timeout() -> None:
    args = parse_args(["serve", "--port", "8123", "--graceful-timeout", "20"])
    assert args.port == 8123
    assert args.graceful_timeout == 20


def test_runtime_cli_requires_subcommand() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_runtime_cli_help_exits_zero(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_args(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "serve" in out


def test_runtime_cli_serve_help_documents_options(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_args(["serve", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--graceful-timeout" in out
    assert "127.0.0.1" in out


# --------------------------------------------------------------------------
# 2. async_main lifecycle (real, in-process, hermetic)
# --------------------------------------------------------------------------


def _hermetic_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # build_agent_runner constructs the read-only graph offline (no live LLM or
    # Houdini call). A unit-test key is enough; mirrors tests/test_harness.py.
    monkeypatch.setenv("EEE_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    monkeypatch.setenv("EEE_COMPACT_TOOL", "false")
    monkeypatch.delenv("EEE_TRACING", raising=False)
    monkeypatch.delenv("EEE_VISION_PROVIDER", raising=False)
    monkeypatch.delenv("EEE_VISION_MODEL", raising=False)
    monkeypatch.delenv("EEE_VISION_MAX_IMAGE_BYTES", raising=False)
    monkeypatch.delenv("EEE_VISION_TIMEOUT_SECONDS", raising=False)


def test_async_main_serves_then_releases_resources_on_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("EEE_RUNTIME_HOME", str(home))
    _hermetic_provider_env(monkeypatch)

    discovery = home / "state" / "runtime.json"
    token_file = home / "state" / "runtime.token"

    async def scenario() -> None:
        task = asyncio.create_task(async_main(["serve"]))
        try:
            for _ in range(200):  # up to ~10s for the server to bind
                if discovery.exists():
                    break
                await asyncio.sleep(0.05)
            assert discovery.exists(), "Runtime did not publish discovery"
            data = json.loads(discovery.read_text(encoding="utf-8"))
            assert data["host"] == "127.0.0.1"
            assert isinstance(data["port"], int) and data["port"] > 0
            token = token_file.read_text(encoding="utf-8")
            # The real server authenticates and serves a ping over the socket.
            async with connect(
                f"ws://{data['host']}:{data['port']}",
                additional_headers={"Authorization": f"Bearer {token}"},
                compression=None,
            ) as ws:
                await ws.send(encode_envelope(_cmd("p1", "runtime.ping", {})))
                pong = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert pong["ok"] is True
        finally:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=15)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    asyncio.run(scenario())

    # Cancellation unwound the lifecycle: discovery + token removed, and the
    # exclusive lock released so a fresh lock acquires immediately.
    assert not discovery.exists()
    assert not token_file.exists()
    paths = RuntimePaths.from_environment()
    with RuntimeLock(paths.lock_file):
        pass


def test_async_main_surfaces_lock_contention_without_half_open_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A second Runtime over the same home cannot acquire the lock; async_main
    # surfaces the structured error (propagates it) and leaves no half-open
    # service/server/identity behind.
    from eee_agent.core import AgentException

    home = tmp_path / "home"
    monkeypatch.setenv("EEE_RUNTIME_HOME", str(home))
    _hermetic_provider_env(monkeypatch)
    paths = RuntimePaths.from_environment()
    paths.create_used_directories()

    lock = RuntimeLock(paths.lock_file)
    lock.__enter__()
    try:
        async def scenario() -> None:
            with pytest.raises(AgentException):
                await async_main(["serve"])

        asyncio.run(scenario())
        # No identity/discovery was written before the lock rejected us.
        assert not (home / "state" / "runtime.json").exists()
        assert not (home / "state" / "runtime.token").exists()
    finally:
        lock.__exit__(None, None, None)

    # After the holder releases, a fresh lock acquires (the failed lifecycle
    # did not leave the OS lock held).
    with RuntimeLock(paths.lock_file):
        pass


# --------------------------------------------------------------------------
# 3. graceful-timeout wiring + identity-cleanup ordering
# --------------------------------------------------------------------------


def test_async_main_passes_graceful_timeout_to_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CLI --graceful-timeout value must reach RuntimeService.open.
    home = tmp_path / "home"
    monkeypatch.setenv("EEE_RUNTIME_HOME", str(home))
    _hermetic_provider_env(monkeypatch)

    from eee_agent.runtime.service import RuntimeService

    original_open = RuntimeService.open
    captured: dict = {}

    def spying_open(*args, **kwargs):
        captured["graceful_timeout"] = kwargs.get("graceful_timeout", "MISSING")
        return original_open(*args, **kwargs)

    monkeypatch.setattr(RuntimeService, "open", spying_open)

    async def scenario() -> None:
        task = asyncio.create_task(async_main(["serve", "--graceful-timeout", "7"]))
        try:
            discovery = home / "state" / "runtime.json"
            for _ in range(200):
                if discovery.exists():
                    break
                await asyncio.sleep(0.05)
            assert discovery.exists(), "Runtime did not start"
        finally:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=15)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    asyncio.run(scenario())
    assert captured.get("graceful_timeout") == 7.0


def test_async_main_passes_vision_timeout_to_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eee_agent.runtime.__main__ as main_mod

    home = tmp_path / "home"
    monkeypatch.setenv("EEE_RUNTIME_HOME", str(home))
    _hermetic_provider_env(monkeypatch)
    captured: dict[str, object] = {}
    original_open = main_mod.RuntimeService.open

    monkeypatch.setattr(main_mod, "_vision_settings", lambda: (None, 12.5))

    def spying_open(*args, **kwargs):
        captured["vision_timeout_seconds"] = kwargs.get(
            "vision_timeout_seconds", "MISSING"
        )
        return original_open(*args, **kwargs)

    monkeypatch.setattr(main_mod.RuntimeService, "open", spying_open)

    async def scenario() -> None:
        task = asyncio.create_task(async_main(["serve"]))
        try:
            discovery = home / "state" / "runtime.json"
            for _ in range(200):
                if discovery.exists():
                    break
                await asyncio.sleep(0.05)
            assert discovery.exists(), "Runtime did not start"
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())
    assert captured["vision_timeout_seconds"] == 12.5


def test_vision_provider_is_disabled_without_explicit_config(monkeypatch) -> None:
    import eee_agent.config as config_module
    import eee_agent.runtime.__main__ as main_mod

    monkeypatch.setattr(config_module, "vision_config", lambda: None)
    assert main_mod._vision_provider() is None
    assert main_mod._vision_settings() == (None, 30.0)


def test_vision_provider_uses_explicit_config_and_registry(monkeypatch) -> None:
    import eee_agent.config as config_module
    import eee_agent.runtime.__main__ as main_mod
    import eee_agent.vision.provider as provider_module
    from eee_agent.config import VisionConfig

    config = VisionConfig("openai", "gpt-4.1", 4096, 17.5)
    sentinel = object()
    seen: list[VisionConfig] = []
    monkeypatch.setattr(config_module, "vision_config", lambda: config)
    monkeypatch.setattr(
        provider_module,
        "build_vision_provider",
        lambda value: seen.append(value) or sentinel,
    )
    assert main_mod._vision_settings() == (sentinel, 17.5)
    assert seen == [config]


def test_async_main_constructs_workspace_provider_from_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import eee_agent.runtime.__main__ as main_mod

    home = tmp_path / "home"
    monkeypatch.setenv("EEE_RUNTIME_HOME", str(home))
    _hermetic_provider_env(monkeypatch)
    sentinel = object()
    constructed: list[Path] = []
    captured: dict = {}
    original_open = main_mod.RuntimeService.open

    def fake_provider(state_dir):
        constructed.append(Path(state_dir))
        return sentinel

    def spying_open(*args, **kwargs):
        captured["workspace_fact_provider"] = kwargs.get(
            "workspace_fact_provider", "MISSING"
        )
        return original_open(*args, **kwargs)

    monkeypatch.setattr(main_mod, "BridgeWorkspaceFactProvider", fake_provider)
    monkeypatch.setattr(main_mod.RuntimeService, "open", spying_open)

    async def scenario() -> None:
        task = asyncio.create_task(async_main(["serve"]))
        try:
            discovery = home / "state" / "runtime.json"
            for _ in range(200):
                if discovery.exists():
                    break
                await asyncio.sleep(0.05)
            assert discovery.exists(), "Runtime did not start"
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())
    assert constructed == [home / "state"]
    assert captured["workspace_fact_provider"] is sentinel


def test_serve_until_shutdown_cleans_identity_after_server_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Identity cleanup must run AFTER the server context exits (server/client
    # resources close first), deterministically, with no real socket or waits.
    import eee_agent.runtime.__main__ as main_mod
    from eee_agent.runtime.auth import create_identity

    log: list[str] = []

    class _FakeServer:
        def __init__(self) -> None:
            self.port = 59999
            self.entered = asyncio.Event()

        async def __aenter__(self) -> "_FakeServer":
            log.append("server_enter")
            self.entered.set()
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            log.append("server_exit")

    def fake_cleanup(identity, state_dir):
        log.append("cleanup")

    monkeypatch.setattr(main_mod, "cleanup_identity_files", fake_cleanup)

    async def scenario() -> None:
        server = _FakeServer()
        identity = create_identity()
        task = asyncio.create_task(
            main_mod._serve_until_shutdown(
                server, identity, tmp_path / "state", host="127.0.0.1"
            )
        )
        await server.entered.wait()  # server entered (deterministic; no sleep)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    # server context exits BEFORE identity files are cleaned up.
    assert log == ["server_enter", "server_exit", "cleanup"]
