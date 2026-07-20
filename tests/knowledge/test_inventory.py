"""Tests for HFS discovery and the hython SOP node-type inventory.

No real Houdini installation is required: HFS roots are built under tmp_path
and the hython subprocess is replaced by an injected runner.
"""

from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path

import pytest

from eee_agent.knowledge.inventory import (
    HfsResolutionError,
    HythonInventoryError,
    HythonInventorySnapshot,
    InventoryError,
    load_inventory_snapshot,
    load_sop_inventory,
    resolve_hfs,
)


def make_fake_hfs(root: Path) -> Path:
    hfs = root / "hfs"
    (hfs / "houdini" / "help").mkdir(parents=True)
    for arc in ("nodes.zip", "hom.zip", "vex.zip"):
        (hfs / "houdini" / "help" / arc).write_bytes(b"")
    (hfs / "bin").mkdir(parents=True)
    (hfs / "bin" / "hython.exe").write_bytes(b"")
    return hfs


def make_invalid_hfs(root: Path) -> Path:
    hfs = root / "invalid"
    hfs.mkdir(parents=True)
    return hfs


def _result(stdout: str = "[]", returncode: int = 0, stderr: str = "") -> object:
    return types.SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def _recording_runner(stdout: str = "[]", returncode: int = 0, stderr: str = "") -> object:
    calls: list[tuple] = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return _result(stdout=stdout, returncode=returncode, stderr=stderr)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _snapshot(
    types=("apex::buildfkgraph", "loadslices"),
    version=(21, 0, 440),
    build: str = "21.0.440",
) -> str:
    """Canned hython snapshot stdout (a strict JSON object)."""
    return json.dumps(
        {"version": list(version), "build": build, "sop_node_types": list(types)}
    )


# --- resolve_hfs ----------------------------------------------------------

def test_explicit_path_wins(tmp_path: Path) -> None:
    fake = make_fake_hfs(tmp_path)
    other = make_invalid_hfs(tmp_path)
    resolved = resolve_hfs(str(fake), {"EEE_HFS": str(other), "HFS": str(other)}, (other,))
    assert resolved == fake


def test_eee_hfs_wins_over_hfs(tmp_path: Path) -> None:
    fake = make_fake_hfs(tmp_path)
    other = make_invalid_hfs(tmp_path)
    resolved = resolve_hfs(None, {"EEE_HFS": str(fake), "HFS": str(other)}, ())
    assert resolved == fake


def test_hfs_env_wins_over_candidates(tmp_path: Path) -> None:
    fake = make_fake_hfs(tmp_path)
    other = make_invalid_hfs(tmp_path)
    resolved = resolve_hfs(None, {"HFS": str(fake)}, (other,))
    assert resolved == fake


def test_known_candidates_preserve_order(tmp_path: Path) -> None:
    fake = make_fake_hfs(tmp_path)
    other = make_invalid_hfs(tmp_path)
    assert resolve_hfs(None, {}, (other, fake)) == fake
    assert resolve_hfs(None, {}, (fake, other)) == fake


def test_missing_all_candidates_raises(tmp_path: Path) -> None:
    other = make_invalid_hfs(tmp_path)
    with pytest.raises(HfsResolutionError):
        resolve_hfs(None, {}, (other,))


def test_required_archive_missing_raises(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    (hfs / "houdini" / "help" / "nodes.zip").unlink()
    with pytest.raises(HfsResolutionError):
        resolve_hfs(str(hfs), {}, ())


def test_hython_executable_missing_raises(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    (hfs / "bin" / "hython.exe").unlink()
    with pytest.raises(HfsResolutionError):
        resolve_hfs(str(hfs), {}, ())


def test_source_path_may_contain_spaces(tmp_path: Path) -> None:
    fake = make_fake_hfs(tmp_path / "hfs with spaces")
    assert resolve_hfs(str(fake), {}, ()) == fake


def test_no_cwd_fallback(tmp_path: Path) -> None:
    with pytest.raises(HfsResolutionError):
        resolve_hfs(None, {}, ())


def test_no_global_environment_mutation(tmp_path: Path, monkeypatch) -> None:
    fake = make_fake_hfs(tmp_path)
    environ = {"EEE_HFS": str(fake)}
    resolve_hfs(None, environ, ())
    assert environ == {"EEE_HFS": str(fake)}
    monkeypatch.setenv("EEE_HFS", str(fake))
    with pytest.raises(HfsResolutionError):
        resolve_hfs(None, {}, ())


def test_explicit_invalid_raises_not_fallthrough(tmp_path: Path) -> None:
    fake = make_fake_hfs(tmp_path)
    invalid = make_invalid_hfs(tmp_path)
    with pytest.raises(HfsResolutionError):
        resolve_hfs(str(invalid), {"EEE_HFS": str(fake)}, ())


def test_eee_hfs_invalid_raises_not_fallthrough(tmp_path: Path) -> None:
    fake = make_fake_hfs(tmp_path)
    invalid = make_invalid_hfs(tmp_path)
    with pytest.raises(HfsResolutionError):
        resolve_hfs(None, {"EEE_HFS": str(invalid), "HFS": str(fake)}, ())


# --- load_inventory_snapshot ----------------------------------------------

def test_snapshot_returns_version_build_and_operators(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    runner = _recording_runner(stdout=_snapshot())
    snap = load_inventory_snapshot(hfs, runner=runner)
    assert isinstance(snap, HythonInventorySnapshot)
    assert snap.houdini_version == "21.0.440"
    assert snap.houdini_build == "21.0.440"
    assert snap.sop_node_types == frozenset({"apex::buildfkgraph", "loadslices"})


def test_snapshot_program_uses_hython_without_shell(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    runner = _recording_runner(stdout=_snapshot())
    load_inventory_snapshot(hfs, runner=runner)
    args, kwargs = runner.calls[0]
    assert args[0] == str(hfs / "bin" / "hython.exe")
    assert args[1] == "-c"
    assert isinstance(args[2], str) and "import hou" in args[2]
    assert "sopNodeTypeCategory" in args[2]
    assert "applicationVersion" in args[2]
    assert kwargs["shell"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 30


def test_snapshot_custom_timeout_passed(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    runner = _recording_runner(stdout=_snapshot())
    load_inventory_snapshot(hfs, runner=runner, timeout_seconds=7)
    assert runner.calls[0][1]["timeout"] == 7


def test_snapshot_timeout_raises(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)

    def runner(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 30))

    with pytest.raises(HythonInventoryError):
        load_inventory_snapshot(hfs, runner=runner, timeout_seconds=5)


def test_snapshot_nonzero_exit_raises(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    runner = _recording_runner(stdout="", returncode=1, stderr="boom")
    with pytest.raises(HythonInventoryError):
        load_inventory_snapshot(hfs, runner=runner)


def test_snapshot_malformed_json_rejected(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_inventory_snapshot(hfs, runner=_recording_runner(stdout="not json"))


def test_snapshot_json_with_log_text_rejected(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_inventory_snapshot(hfs, runner=_recording_runner(stdout=_snapshot() + " log"))


def test_snapshot_top_level_must_be_object(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_inventory_snapshot(hfs, runner=_recording_runner(stdout='["a"]'))


def test_snapshot_version_must_be_three_ints(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_inventory_snapshot(hfs, runner=_recording_runner(stdout=_snapshot(version=[21, 0])))
    with pytest.raises(HythonInventoryError):
        load_inventory_snapshot(
            hfs, runner=_recording_runner(stdout=_snapshot(version=[21, "0", 440]))
        )


def test_snapshot_build_must_be_consistent_with_version(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_inventory_snapshot(
            hfs, runner=_recording_runner(stdout=_snapshot(build="21.0.500"))
        )


def test_snapshot_build_must_be_non_empty(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_inventory_snapshot(hfs, runner=_recording_runner(stdout=_snapshot(build="")))


def test_snapshot_types_must_be_non_empty_array(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_inventory_snapshot(hfs, runner=_recording_runner(stdout=_snapshot(types=())))


def test_snapshot_non_string_type_rejected(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    payload = json.dumps(
        {"version": [21, 0, 440], "build": "21.0.440", "sop_node_types": [1, "a"]}
    )
    with pytest.raises(HythonInventoryError):
        load_inventory_snapshot(hfs, runner=_recording_runner(stdout=payload))


def test_snapshot_empty_type_name_rejected(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_inventory_snapshot(hfs, runner=_recording_runner(stdout=_snapshot(types=("a", ""))))


def test_snapshot_control_char_in_type_rejected(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    # A control character (SOH) inside a type name must be rejected.
    payload = _snapshot(types=("a" + chr(1) + "b",))
    with pytest.raises(HythonInventoryError):
        load_inventory_snapshot(hfs, runner=_recording_runner(stdout=payload))


def test_snapshot_duplicates_collapse(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    runner = _recording_runner(stdout=_snapshot(types=("a", "a", "b")))
    snap = load_inventory_snapshot(hfs, runner=runner)
    assert snap.sop_node_types == frozenset({"a", "b"})


def test_snapshot_hython_missing_raises(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    (hfs / "bin" / "hython.exe").unlink()
    with pytest.raises(HythonInventoryError):
        load_inventory_snapshot(hfs, runner=_recording_runner(stdout=_snapshot()))


def test_snapshot_no_absolute_path_leaked(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    runner = _recording_runner(stdout=_snapshot())
    snap = load_inventory_snapshot(hfs, runner=runner)
    for name in snap.sop_node_types:
        assert "/" not in name
        assert "\\" not in name


def test_snapshot_errors_are_contextual(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError) as exc_info:
        load_inventory_snapshot(hfs, runner=_recording_runner(stdout="not json"))
    assert isinstance(exc_info.value, InventoryError)


# --- load_sop_inventory (compat facade) -----------------------------------

def test_load_sop_inventory_returns_only_operators(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    runner = _recording_runner(stdout=_snapshot())
    result = load_sop_inventory(hfs, runner=runner)
    assert isinstance(result, frozenset)
    assert result == frozenset({"apex::buildfkgraph", "loadslices"})


def test_load_sop_inventory_custom_timeout(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    runner = _recording_runner(stdout=_snapshot())
    load_sop_inventory(hfs, runner=runner, timeout_seconds=7)
    assert runner.calls[0][1]["timeout"] == 7


def test_load_sop_inventory_propagates_errors(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_sop_inventory(hfs, runner=_recording_runner(stdout="not json"))
