import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


PROBE = Path("scripts/env_probe.sh")
ATTRIBUTES = Path(".gitattributes")


def _bash_command() -> str:
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if os.name == "nt" and git_bash.is_file():
        return str(git_bash)
    return "bash"


def _run_probe_with_env_file(contents: bytes) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix=".env-probe-test-", dir=Path.cwd()) as tmp:
        env_file = Path(tmp, "fixture.env")
        env_file.write_bytes(contents)
        env = os.environ.copy()
        env["EEE_PROBE_ENV_FILE"] = env_file.relative_to(Path.cwd()).as_posix()
        return subprocess.run(
            [_bash_command(), "-x", PROBE.as_posix()],
            cwd=Path.cwd(),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )


def _wsl_bash_prefix() -> list[str]:
    if os.name != "nt":
        pytest.skip("WSL-to-Windows probe requires Windows")
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    legacy_bash = system_root / "System32" / "bash.exe"
    if legacy_bash.is_file():
        prefix = [str(legacy_bash)]
    else:
        wsl = shutil.which("wsl.exe")
        if wsl is None:
            pytest.skip("WSL Bash is unavailable")
        prefix = [wsl, "bash"]
    availability = subprocess.run(
        [
            *prefix,
            "-c",
            "test -d /mnt && test -f .env.example && "
            "test -x .venv/Scripts/python.exe && "
            "'.venv/Scripts/python.exe' -B -c 'pass'",
        ],
        cwd=Path.cwd(),
        capture_output=True,
        check=False,
    )
    if availability.returncode != 0:
        pytest.skip("WSL or the Windows probe venv is unavailable")
    return prefix


def test_wsl_availability_probe_executes_windows_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def record_run(args: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", record_run)
    _wsl_bash_prefix()
    assert "'.venv/Scripts/python.exe' -B -c 'pass'" in calls[0][-1]


def test_probe_is_tracked_in_the_foundation_branch() -> None:
    assert PROBE.is_file()


def test_shell_scripts_are_checked_out_with_lf_endings() -> None:
    attributes = ATTRIBUTES.read_text(encoding="utf-8").splitlines()
    assert "*.sh text eol=lf" in attributes


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


def test_probe_sets_placeholder_inside_python_harness() -> None:
    text = PROBE.read_text(encoding="utf-8")
    assert (
        "os.environ.setdefault('DEEPSEEK_API_KEY', 'probe-placeholder-not-used')"
        in text
    )
    shell_assignment = (
        'DEEPSEEK_API_KEY="'
        "${DEEPSEEK_API_KEY:-probe-placeholder-not-used}"
        '"'
    )
    assert shell_assignment not in text


def test_probe_python_processes_are_side_effect_free() -> None:
    text = PROBE.read_text(encoding="utf-8")
    python_flags = re.findall(
        r'"(?:\$[A-Z_]+(?:/[^"\n]*)?|[^"\n]*python\.exe)" '
        r'(?:(-B) )?(?:-c|--version)',
        text,
    )
    assert len(python_flags) >= 5
    assert all(flag == "-B" for flag in python_flags)

    harness = text[text.index("printf 'agent harness:") :]
    import_agent = "from eee_agent.app import build_agent"
    side_effect_guards = (
        "os.environ['EEE_CONTEXTSEEK'] = 'false'",
        "os.environ['EEE_TRACING'] = ''",
        "os.environ.setdefault('DEEPSEEK_API_KEY', 'probe-placeholder-not-used')",
    )
    assert import_agent in harness
    assert all(guard in harness for guard in side_effect_guards)
    assert all(harness.index(guard) < harness.index(import_agent) for guard in side_effect_guards)
    assert "set +e" in text
    assert text.rstrip().endswith("exit 0")


def test_probe_uses_python_dotenv_without_shell_secret() -> None:
    text = PROBE.read_text(encoding="utf-8")
    assert "from dotenv import dotenv_values" in text
    assert "KEY=$(" not in text
    assert "grep -E '^DEEPSEEK_API_KEY='" not in text


@pytest.mark.parametrize(
    ("contents", "expected_status"),
    [
        pytest.param(
            b'OTHER=value\r\nDEEPSEEK_API_KEY=""\r\n',
            "[WARN] DEEPSEEK_API_KEY is empty or a placeholder",
            id="crlf-quoted-empty",
        ),
        pytest.param(
            b'DEEPSEEK_API_KEY="sk-your-deepseek-key"\n',
            "[WARN] DEEPSEEK_API_KEY is empty or a placeholder",
            id="quoted-placeholder",
        ),
        pytest.param(
            b"OTHER=value\n",
            "[WARN] DEEPSEEK_API_KEY is missing",
            id="missing-key",
        ),
        pytest.param(
            b'DEEPSEEK_API_KEY="sk-your-deepseek-key"\n'
            b'DEEPSEEK_API_KEY="unit-test-secret"\n',
            "[ok]   DEEPSEEK_API_KEY is set",
            id="duplicate-effective-last-value",
        ),
    ],
)
def test_probe_reports_effective_dotenv_key_status(
    contents: bytes, expected_status: str
) -> None:
    result = _run_probe_with_env_file(contents)
    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert expected_status in output
    assert "unit-test-secret" not in output


def test_wsl_probe_normalizes_windows_python_output() -> None:
    result = subprocess.run(
        [
            *_wsl_bash_prefix(),
            "-c",
            "EEE_PROBE_ENV_FILE=.env.example bash scripts/env_probe.sh",
        ],
        cwd=Path.cwd(),
        capture_output=True,
        check=False,
    )
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    assert result.returncode == 0
    assert "[WARN] DEEPSEEK_API_KEY is empty or a placeholder" in output
    assert "could not inspect DEEPSEEK_API_KEY" not in output
    assert "\r" not in output
