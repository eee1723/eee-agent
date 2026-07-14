from pathlib import Path

import pytest

from eee_agent.runtime.paths import RuntimePaths


def test_override_builds_expected_paths_without_creating_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "runtime-home"
    monkeypatch.setenv("EEE_RUNTIME_HOME", str(home))
    paths = RuntimePaths.from_environment()
    assert paths.home == home.resolve()
    assert paths.app_db == home.resolve() / "state" / "app.sqlite"
    assert paths.checkpoints_db == home.resolve() / "state" / "checkpoints.sqlite"
    assert paths.discovery_file == home.resolve() / "state" / "runtime.json"
    assert not home.exists()


def test_relative_override_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EEE_RUNTIME_HOME", "relative/runtime")
    with pytest.raises(ValueError, match="absolute"):
        RuntimePaths.from_environment()


def test_parent_traversal_override_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An absolute override that hides a `..` segment must be rejected before
    # resolve() normalizes it away (approved spec rejects parent traversal).
    unsafe = tmp_path / ".." / "escape"
    assert ".." in unsafe.parts
    monkeypatch.setenv("EEE_RUNTIME_HOME", str(unsafe))
    with pytest.raises(ValueError, match="parent traversal"):
        RuntimePaths.from_environment()


def test_empty_override_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EEE_RUNTIME_HOME", "")
    with pytest.raises(ValueError, match="empty"):
        RuntimePaths.from_environment()


def test_override_pointing_at_a_regular_file_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_home = tmp_path / "not-a-dir"
    file_home.write_text("not a directory")
    monkeypatch.setenv("EEE_RUNTIME_HOME", str(file_home))
    with pytest.raises(ValueError, match="directory"):
        RuntimePaths.from_environment()


def test_default_uses_local_app_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EEE_RUNTIME_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert RuntimePaths.from_environment().home == (tmp_path / "EEEAgent").resolve()
