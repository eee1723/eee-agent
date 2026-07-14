"""Tests for HFS discovery and the hython SOP node-type inventory.

No real Houdini installation is required: HFS roots are built under tmp_path
and the hython subprocess is replaced by an injected runner.
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from eee_agent.knowledge.inventory import (
    HfsResolutionError,
    HythonInventoryError,
    InventoryError,
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


# --- load_sop_inventory ---------------------------------------------------

def test_inventory_uses_hython_without_shell(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    runner = _recording_runner(stdout='["apex::buildfkgraph", "loadslices"]')
    inventory = load_sop_inventory(hfs, runner=runner)
    assert inventory == frozenset({"apex::buildfkgraph", "loadslices"})
    args, kwargs = runner.calls[0]
    assert args[0] == str(hfs / "bin" / "hython.exe")
    assert args[1] == "-c"
    assert isinstance(args[2], str) and "import hou" in args[2]
    assert "sopNodeTypeCategory" in args[2]
    assert kwargs["shell"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 30


def test_inventory_custom_timeout_passed(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    runner = _recording_runner(stdout="[]")
    load_sop_inventory(hfs, runner=runner, timeout_seconds=7)
    assert runner.calls[0][1]["timeout"] == 7


def test_inventory_timeout_raises(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)

    def runner(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 30))

    with pytest.raises(HythonInventoryError):
        load_sop_inventory(hfs, runner=runner, timeout_seconds=5)


def test_inventory_nonzero_exit_raises(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    runner = _recording_runner(stdout="", returncode=1, stderr="boom")
    with pytest.raises(HythonInventoryError):
        load_sop_inventory(hfs, runner=runner)


def test_inventory_malformed_json_rejected(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_sop_inventory(hfs, runner=_recording_runner(stdout="not json"))


def test_inventory_json_with_log_text_rejected(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_sop_inventory(hfs, runner=_recording_runner(stdout='["a"] extra log'))


def test_inventory_top_level_must_be_list(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_sop_inventory(hfs, runner=_recording_runner(stdout='"notalist"'))


def test_inventory_non_string_member_rejected(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_sop_inventory(hfs, runner=_recording_runner(stdout='[1, "a"]'))


def test_inventory_empty_name_rejected(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_sop_inventory(hfs, runner=_recording_runner(stdout='[""]'))


def test_inventory_control_char_rejected(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError):
        load_sop_inventory(hfs, runner=_recording_runner(stdout='["a\\u0000b"]'))


def test_inventory_duplicates_collapse(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    runner = _recording_runner(stdout='["a", "a", "b"]')
    assert load_sop_inventory(hfs, runner=runner) == frozenset({"a", "b"})


def test_inventory_hython_missing_raises(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    (hfs / "bin" / "hython.exe").unlink()
    with pytest.raises(HythonInventoryError):
        load_sop_inventory(hfs, runner=_recording_runner())


def test_inventory_no_absolute_path_leaked(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    runner = _recording_runner(stdout='["apex::buildfkgraph", "loadslices"]')
    inventory = load_sop_inventory(hfs, runner=runner)
    for name in inventory:
        assert "/" not in name
        assert "\\" not in name


def test_inventory_errors_are_contextual(tmp_path: Path) -> None:
    hfs = make_fake_hfs(tmp_path)
    with pytest.raises(HythonInventoryError) as exc_info:
        load_sop_inventory(hfs, runner=_recording_runner(stdout="not json"))
    assert isinstance(exc_info.value, InventoryError)
