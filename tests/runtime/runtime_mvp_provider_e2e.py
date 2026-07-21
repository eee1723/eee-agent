"""Opt-in provider/Houdini Runtime MVP acceptance runner.

This runner is deliberately separate from the deterministic pytest suite.  It
never infers that a provider or Houdini is available, and it never prints the
provider process output (which could contain credentials or prompt content).
Without all explicit gates it emits a bounded ``not run`` record and exits
successfully so offline CI remains truthful rather than red.

Usage (PowerShell)::

    $env:EEE_RUN_RUNTIME_MVP_PROVIDER_E2E = "true"
    $env:EEE_RUNTIME_MVP_PROVIDER_COMMAND = "python path\\to\\provider_journey.py"
    python tests/runtime/runtime_mvp_provider_e2e.py

The command receives ``EEE_RUNTIME_HOME`` and ``EEE_RUNTIME_MVP_HIP_PATH`` in a
disposable temporary directory. It must write a strict bounded JSON evidence
record to ``EEE_RUNTIME_MVP_EVIDENCE_PATH``. A zero exit without that evidence
is a failure; provider output is intentionally not persisted or displayed.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


_OPT_IN = "EEE_RUN_RUNTIME_MVP_PROVIDER_E2E"
_COMMAND = "EEE_RUNTIME_MVP_PROVIDER_COMMAND"
_HFS_VARS = ("HFS", "HOUDINI_HOME")
_CREDENTIAL_VARS = (
    "EEE_LLM_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
)
_MAX_EVIDENCE_BYTES = 16 * 1024
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_FIELDS = frozenset(
    {
        "proposal_digest",
        "approval_event",
        "receipt_status",
        "validation_status",
        "artifact_status",
        "replay_last_seq",
        "scene_cleanup",
    }
)


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _hfs_executable() -> Path | None:
    for name in _HFS_VARS:
        raw = os.getenv(name)
        if not raw:
            continue
        root = Path(raw)
        candidate = root / "bin" / ("hython.exe" if os.name == "nt" else "hython")
        if candidate.is_file():
            return candidate
    return None


def _record(status: str, reason: str, *, hfs: bool = False, provider: bool = False) -> int:
    # Keep this schema intentionally small.  In particular, do not include
    # environment values, command text, process output, or temporary paths.
    print(
        json.dumps(
            {
                "acceptance": "runtime-mvp-provider",
                "status": status,
                "reason": reason,
                "hfs_available": hfs,
                "provider_credentials_available": provider,
            },
            sort_keys=True,
        )
    )
    return 0 if status in {"passed", "not_run"} else 1


def _validate_evidence(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_EVIDENCE_BYTES + 1)
        if not raw or len(raw) > _MAX_EVIDENCE_BYTES:
            return False
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if type(payload) is not dict or frozenset(payload) != _EVIDENCE_FIELDS:
        return False
    return (
        type(payload["proposal_digest"]) is str
        and _DIGEST_RE.fullmatch(payload["proposal_digest"]) is not None
        and payload["approval_event"] == "approved"
        and payload["receipt_status"] in {"applied", "already_applied"}
        and payload["validation_status"] == "passed"
        and payload["artifact_status"] == "available"
        and type(payload["replay_last_seq"]) is int
        and payload["replay_last_seq"] >= 1
        and payload["scene_cleanup"] == "completed"
    )


def main() -> int:
    if not _enabled(os.getenv(_OPT_IN)):
        return _record("not_run", "explicit_opt_in_required")

    hfs = _hfs_executable()
    credentials = any(bool(os.getenv(name, "").strip()) for name in _CREDENTIAL_VARS)
    if hfs is None:
        return _record("not_run", "houdini_hfs_unavailable", provider=credentials)
    if not credentials:
        return _record("not_run", "provider_credentials_unavailable", hfs=True)

    command = os.getenv(_COMMAND, "").strip()
    if not command:
        return _record("not_run", "provider_journey_command_required", hfs=True, provider=True)
    try:
        argv = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return _record("not_run", "provider_journey_command_invalid", hfs=True, provider=True)
    if not argv:
        return _record("not_run", "provider_journey_command_required", hfs=True, provider=True)

    # The command is the provider-specific adapter.  The harness supplies only
    # disposable state and the HFS location; inherited credentials are needed
    # by the adapter but are never echoed by this process.
    with tempfile.TemporaryDirectory(prefix="eee-runtime-mvp-") as temp:
        evidence_path = Path(temp) / "evidence.json"
        env = dict(os.environ)
        env["EEE_RUNTIME_HOME"] = str(Path(temp) / "runtime")
        env["EEE_RUNTIME_MVP_HFS"] = str(hfs)
        env["EEE_RUNTIME_MVP_HIP_PATH"] = str(Path(temp) / "scene.hip")
        env["EEE_RUNTIME_MVP_EVIDENCE_PATH"] = str(evidence_path)
        env["EEE_RUNTIME_MVP_ACCEPTANCE"] = "1"
        try:
            completed = subprocess.run(
                argv,
                cwd=str(Path.cwd()),
                env=env,
                stdin=subprocess.DEVNULL,
                # Provider output can contain unbounded model text or secrets;
                # evidence must be emitted by the adapter as bounded records,
                # never collected by this harness.
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=900,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return _record("failed", "provider_journey_process_error", hfs=True, provider=True)
        if completed.returncode != 0:
            return _record("failed", "provider_journey_failed", hfs=True, provider=True)
        if not _validate_evidence(evidence_path):
            return _record(
                "failed", "provider_journey_evidence_invalid", hfs=True, provider=True
            )

    return _record("passed", "provider_journey_completed", hfs=True, provider=True)


if __name__ == "__main__":
    raise SystemExit(main())
