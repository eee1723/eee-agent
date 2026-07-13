from pathlib import Path


PROBE = Path("scripts/env_probe.sh")


def test_probe_is_tracked_in_the_foundation_branch() -> None:
    assert PROBE.is_file()


def test_probe_checks_wsl_mount_before_git_bash_mount() -> None:
    text = PROBE.read_text(encoding="utf-8")
    assert '"/mnt/d/houdini"' in text
    assert '"/d/houdini"' in text
    assert text.index('"/mnt/d/houdini"') < text.index('"/d/houdini"')


def test_probe_remains_read_only_and_non_fatal() -> None:
    text = PROBE.read_text(encoding="utf-8")
    assert "set +e" in text
    assert "rm -" not in text
    assert "git clean" not in text
