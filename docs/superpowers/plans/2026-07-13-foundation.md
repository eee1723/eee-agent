# Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a reproducible, tested foundation with stable core contracts, a provider-neutral model registry, a correct DeepSeek V4 Anthropic adapter, normalized provider events, and explicit Deep Agents harness behavior.

**Architecture:** Add two inward-facing packages: `eee_agent.core` owns provider-independent IDs, errors, events, artifacts, and version reports; `eee_agent.providers` owns model connection/profile contracts and provider adapters. Preserve the current CLI and `build_agent()` entry point, but route model construction through the registry and disable Deep Agents' implicit `general-purpose` subagent until a restricted general capability is designed.

**Tech Stack:** Python 3.11.7, uv 0.11, pytest 8.4, Deep Agents 0.6.12, LangChain 1.3.13, LangGraph 1.2.9, `langchain-anthropic` 1.4.8, `langchain-openai` 1.3.5, rpyc 4.1.0.

---

## Scope And Preconditions

- Execute in `E:\eee-agent\.worktrees\foundation` on branch `feature/foundation`.
- The approved master design is `docs/superpowers/specs/2026-07-13-houdini-general-agent-architecture-design.md`.
- Do not implement Runtime, SQLite persistence, WebSocket protocol, Restricted HoudiniBridge, docked UI, strict modeling validators, capture, vision, Phoenix changes, or LangSmith export in this plan.
- Do not touch `README.md`, `CLAUDE.md`, or `houdini_side/install_menu.py`; the main worktree has user modifications in those files.
- The main worktree has an untracked `scripts/env_probe.sh`. Task 2 creates the tracked branch version from the captured content and adds `/mnt/d/houdini`; do not delete or overwrite the main-worktree copy during implementation.
- Unit and contract tests must not call a live LLM or require Houdini. Use placeholder API keys only in process-local test environments.
- Do not expose API key values in assertion messages, logs, snapshots, or commits.

## Authoritative References

- DeepSeek Anthropic compatibility and supported fields: <https://api-docs.deepseek.com/guides/anthropic_api/>
- DeepSeek thinking toggle, effort, and tool replay: <https://api-docs.deepseek.com/guides/thinking_mode/>
- Deep Agents default and disabled subagent behavior: <https://docs.langchain.com/oss/python/deepagents/subagents>
- LangChain Anthropic integration: <https://docs.langchain.com/oss/python/integrations/chat/anthropic>

The pinned package source is the executable contract for private compatibility checks such as `ChatAnthropic._get_request_payload` and compiled ToolNode inspection. Those checks are intentionally narrow and must fail loudly when a dependency upgrade changes the private surface.

## File Map

### Create

- `uv.lock`: complete reproducible dependency graph.
- `tests/test_dependency_baseline.py`: lock and direct dependency regression tests.
- `tests/test_env_probe.py`: environment-probe path regression tests.
- `tests/core/test_ids.py`: typed ID tests.
- `tests/core/test_errors.py`: structured error tests.
- `tests/core/test_artifacts.py`: artifact-reference validation tests.
- `tests/core/test_events.py`: core event serialization tests.
- `tests/core/test_versioning.py`: runtime version-report tests.
- `tests/providers/test_contracts.py`: provider contract and registry tests.
- `tests/providers/test_deepseek_v4.py`: DeepSeek adapter and Anthropic payload tests.
- `tests/providers/test_factory.py`: environment-to-provider factory tests.
- `tests/providers/test_events.py`: provider event-normalization tests.
- `tests/test_cli_stream_events.py`: normalized-event to legacy stdio compatibility tests.
- `tests/test_harness.py`: explicit Deep Agents harness regression test.
- `tests/test_cli_versions.py`: CLI version-report test.
- `scripts/env_probe.sh`: tracked, read-only environment probe.
- `eee_agent/core/__init__.py`: public core contract exports.
- `eee_agent/core/ids.py`: entity ID kinds, validation, and factories.
- `eee_agent/core/errors.py`: provider-neutral structured errors.
- `eee_agent/core/artifacts.py`: immutable artifact references.
- `eee_agent/core/events.py`: provider-neutral domain event envelope.
- `eee_agent/core/versioning.py`: dependency and runtime version report.
- `eee_agent/providers/__init__.py`: public provider exports.
- `eee_agent/providers/contracts.py`: connections, profiles, capabilities, roles, adapter protocol.
- `eee_agent/providers/registry.py`: adapter registration and model resolution.
- `eee_agent/providers/secrets.py`: environment-backed secret reference resolution.
- `eee_agent/providers/deepseek_v4.py`: official DeepSeek Anthropic adapter.
- `eee_agent/providers/anthropic.py`: standard Anthropic adapter.
- `eee_agent/providers/openai.py`: standard OpenAI adapter.
- `eee_agent/providers/factory.py`: registry construction and environment-config resolution.
- `eee_agent/providers/events.py`: normalized provider event types.
- `eee_agent/providers/normalize.py`: LangChain message-chunk normalization.
- `eee_agent/harness.py`: explicit Deep Agents harness profile registration.

### Modify

- `pyproject.toml`: exact direct dependency versions, pytest extra, pytest configuration.
- `.env.example`: DeepSeek Anthropic transport and thinking settings.
- `eee_agent/config.py`: extend `LlmConfig` with validated thinking, effort, and output limit.
- `eee_agent/model.py`: replace direct provider branching with ProviderRegistry.
- `eee_agent/app.py`: configure the Deep Agents harness before graph construction.
- `eee_agent/cli.py`: add a machine-readable `versions` command.

### Task 1: Reproducible Dependencies And Pytest Baseline

**Files:**
- Modify: `pyproject.toml`
- Create: `uv.lock`
- Create: `tests/test_dependency_baseline.py`

- [ ] **Step 1: Write the failing lock test**

Create `tests/test_dependency_baseline.py`:

```python
from importlib import metadata
from pathlib import Path

import pytest


EXPECTED_DIRECT_VERSIONS = {
    "deepagents": "0.6.12",
    "langchain": "1.3.13",
    "langchain-core": "1.4.9",
    "langchain-openai": "1.3.5",
    "langchain-anthropic": "1.4.8",
    "langgraph": "1.2.9",
    "rpyc": "4.1.0",
    "pyyaml": "6.0.3",
    "python-dotenv": "1.2.2",
}


def test_uv_lock_is_committed() -> None:
    assert Path("uv.lock").is_file()


@pytest.mark.parametrize(("distribution", "expected"), EXPECTED_DIRECT_VERSIONS.items())
def test_direct_dependency_version(distribution: str, expected: str) -> None:
    assert metadata.version(distribution) == expected
```

- [ ] **Step 2: Run the test to verify the missing lock fails**

Run:

```powershell
uv run --extra eval pytest tests/test_dependency_baseline.py::test_uv_lock_is_committed -v
```

Expected: `FAIL` because `uv.lock` does not exist.

- [ ] **Step 3: Replace open dependency ranges with the verified direct versions**

Replace `pyproject.toml` with:

```toml
[project]
name = "eee-agent"
version = "0.1.0"
description = "deepagents-based procedural modeling agent for SideFX Houdini 21"
requires-python = ">=3.11,<3.12"
dependencies = [
    "deepagents==0.6.12",
    "langchain==1.3.13",
    "langchain-core==1.4.9",
    "langchain-openai==1.3.5",
    "langchain-anthropic==1.4.8",
    "langgraph==1.2.9",
    # Houdini 21.0.440 bundles rpyc 4.1.0. The wire protocol must match.
    "rpyc==4.1.0",
    "pyyaml==6.0.3",
    "python-dotenv==1.2.2",
]

[project.optional-dependencies]
eval = ["pytest==8.4.1"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["eee_agent*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
```

- [ ] **Step 4: Generate and sync the lock**

Run:

```powershell
uv lock --python 3.11
uv sync --extra eval --python 3.11
```

Expected: `uv.lock` is created and `.venv` contains pytest 8.4.1 plus the exact direct versions.

- [ ] **Step 5: Run dependency tests**

Run:

```powershell
uv run --extra eval pytest tests/test_dependency_baseline.py -v
uv lock --check
```

Expected: all dependency tests pass and `uv lock --check` exits 0.

- [ ] **Step 6: Commit the dependency baseline**

```powershell
git add pyproject.toml uv.lock tests/test_dependency_baseline.py
git commit -m "build: lock foundation dependencies"
```

### Task 2: Track And Correct The Environment Probe

**Files:**
- Create: `scripts/env_probe.sh`
- Create: `tests/test_env_probe.py`

- [ ] **Step 1: Write the failing path-regression tests**

Create `tests/test_env_probe.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify the branch is missing the probe**

Run:

```powershell
uv run --extra eval pytest tests/test_env_probe.py -v
```

Expected: `FAIL` because `scripts/env_probe.sh` is absent in the worktree.

- [ ] **Step 3: Create the tracked probe with correct cross-shell candidates**

Create `scripts/env_probe.sh`:

```bash
#!/usr/bin/env bash
# EEE Agent environment probe. It is read-only and never fails the session.
set +e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT" 2>/dev/null

ok()   { printf '  [ok]   %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
na()   { printf '  [ -- ] %s\n' "$*"; }

printf '=== EEE Agent environment probe | host=%s | %s ===\n' \
  "$(hostname 2>/dev/null)" "$(date '+%F %T' 2>/dev/null)"
printf 'repo: %s\n' "$ROOT"

printf 'Houdini 21 install:\n'
HOU=""
for cand in \
  "${HOUDINI_ROOT:-}" \
  "/mnt/d/houdini" \
  "/d/houdini" \
  "/c/Program Files/Side Effects Software/Houdini 21.0.440"
do
  if [ -n "$cand" ] && [ -f "$cand/bin/hython.exe" ]; then
    ok "found at $cand"
    HOU="$cand"
    break
  fi
done
if [ -z "$HOU" ]; then
  warn "not at known paths; set HOUDINI_ROOT when Houdini is installed elsewhere"
fi

HOU_RPYC=""
if [ -n "$HOU" ] && [ -x "$HOU/python311/python.exe" ]; then
  HOU_RPYC=$("$HOU/python311/python.exe" -c \
    "import rpyc; print('.'.join(map(str, rpyc.version.version)))" 2>/dev/null)
  [ -n "$HOU_RPYC" ] && ok "Houdini bundled rpyc = $HOU_RPYC" || \
    na "could not read Houdini rpyc version"
fi

printf 'agent venv:\n'
VENV_PY=""
VENV_RPYC=""
if [ -x ".venv/Scripts/python.exe" ]; then
  VENV_PY=".venv/Scripts/python.exe"
  ok ".venv present ($("$VENV_PY" --version 2>&1))"
  VENV_RPYC=$("$VENV_PY" -c \
    "import rpyc; print('.'.join(map(str, rpyc.version.version)))" 2>/dev/null)
  if [ -n "$VENV_RPYC" ]; then
    ok "venv rpyc = $VENV_RPYC"
    if [ -n "$HOU_RPYC" ] && [ "$HOU_RPYC" != "$VENV_RPYC" ]; then
      warn "rpyc mismatch: venv $VENV_RPYC vs Houdini $HOU_RPYC"
    fi
  else
    warn ".venv exists but rpyc is missing; run uv sync --extra eval"
  fi
else
  warn ".venv missing; run uv sync --extra eval"
fi

printf 'config / keys:\n'
if [ -f ".env" ]; then
  ok ".env present"
  KEY=$(grep -E '^DEEPSEEK_API_KEY=' .env 2>/dev/null | head -1 | cut -d= -f2-)
  case "$KEY" in
    ""|"sk-your-deepseek-key") warn "DEEPSEEK_API_KEY is empty or a placeholder" ;;
    *) ok "DEEPSEEK_API_KEY is set" ;;
  esac
else
  warn ".env missing; create it from .env.example"
fi

PORTPY="$VENV_PY"
if [ -z "$PORTPY" ] && [ -n "$HOU" ] && [ -x "$HOU/python311/python.exe" ]; then
  PORTPY="$HOU/python311/python.exe"
fi

printf 'Houdini RPC bridge (127.0.0.1:18811):\n'
if [ -n "$PORTPY" ]; then
  if "$PORTPY" -c "import socket
s = socket.socket()
s.settimeout(1.0)
try:
    s.connect(('127.0.0.1', 18811))
except Exception:
    raise SystemExit(1)
finally:
    s.close()" 2>/dev/null; then
    ok "UP"
  else
    na "not listening; only required for Houdini integration runs"
  fi
else
  na "no Python available to test the port"
fi

printf 'agent harness:\n'
if [ -n "$VENV_PY" ]; then
  if DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-probe-placeholder-not-used}" \
    "$VENV_PY" -c "from eee_agent.app import build_agent; build_agent()" \
    >/dev/null 2>&1; then
    ok "build_agent() compiles"
  else
    warn "build_agent() failed; run uv sync --extra eval and inspect the error"
  fi
else
  na "skip harness check because the venv is unavailable"
fi

printf '=== end probe ===\n'
exit 0
```

- [ ] **Step 4: Run probe tests and the local probe**

Run:

```powershell
uv run --extra eval pytest tests/test_env_probe.py -v
bash scripts/env_probe.sh
```

Expected: tests pass; the probe reports Houdini at `/mnt/d/houdini` on this machine and exits 0 even if RPC is down.

- [ ] **Step 5: Commit the tracked probe**

```powershell
git add scripts/env_probe.sh tests/test_env_probe.py
git commit -m "fix: make environment probe shell-portable"
```

### Task 3: Typed Entity IDs

**Files:**
- Create: `eee_agent/core/__init__.py`
- Create: `eee_agent/core/ids.py`
- Create: `tests/core/test_ids.py`

- [ ] **Step 1: Write failing ID tests**

Create `tests/core/test_ids.py`:

```python
import pytest

from eee_agent.core.ids import IdKind, new_id, require_id


def test_new_id_has_kind_prefix_and_uuid_payload() -> None:
    value = new_id(IdKind.SESSION)
    prefix, payload = value.split("_", 1)
    assert prefix == "ses"
    assert len(payload) == 32
    int(payload, 16)


def test_require_id_rejects_wrong_kind() -> None:
    with pytest.raises(ValueError, match="expected run_"):
        require_id("ses_0123456789abcdef0123456789abcdef", IdKind.RUN)


def test_require_id_rejects_non_hex_payload() -> None:
    with pytest.raises(ValueError, match="invalid artifact id"):
        require_id("art_not-hex", IdKind.ARTIFACT)
```

- [ ] **Step 2: Run tests to verify the package is absent**

Run:

```powershell
uv run --extra eval pytest tests/core/test_ids.py -v
```

Expected: collection fails with `ModuleNotFoundError: eee_agent.core`.

- [ ] **Step 3: Implement ID kinds and validation**

Create `eee_agent/core/ids.py`:

```python
from __future__ import annotations

import re
from enum import StrEnum
from uuid import uuid4


class IdKind(StrEnum):
    SESSION = "ses"
    RUN = "run"
    EVENT = "evt"
    WORKSPACE = "ws"
    CHANGE = "chg"
    ARTIFACT = "art"


_PAYLOAD_RE = re.compile(r"^[0-9a-f]{32}$")


def new_id(kind: IdKind) -> str:
    return f"{kind.value}_{uuid4().hex}"


def require_id(value: str, kind: IdKind) -> str:
    prefix = f"{kind.value}_"
    if not value.startswith(prefix):
        raise ValueError(f"expected {prefix} id, got {value!r}")
    payload = value[len(prefix):]
    if not _PAYLOAD_RE.fullmatch(payload):
        raise ValueError(f"invalid {kind.name.lower()} id: {value!r}")
    return value
```

Create `eee_agent/core/__init__.py`:

```python
from eee_agent.core.ids import IdKind, new_id, require_id

__all__ = ["IdKind", "new_id", "require_id"]
```

- [ ] **Step 4: Run ID tests**

Run:

```powershell
uv run --extra eval pytest tests/core/test_ids.py -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit ID contracts**

```powershell
git add eee_agent/core tests/core/test_ids.py
git commit -m "feat: add typed foundation ids"
```

### Task 4: Structured Errors And Artifact References

**Files:**
- Create: `eee_agent/core/errors.py`
- Create: `eee_agent/core/artifacts.py`
- Modify: `eee_agent/core/__init__.py`
- Create: `tests/core/test_errors.py`
- Create: `tests/core/test_artifacts.py`

- [ ] **Step 1: Write failing error and artifact tests**

Create `tests/core/test_errors.py`:

```python
import pytest

from eee_agent.core.errors import AgentError, AgentException, ErrorCategory


def test_agent_error_serializes_without_exception_objects() -> None:
    error = AgentError(
        code="provider.missing_key",
        category=ErrorCategory.PROVIDER_CONTRACT,
        message_for_user="DeepSeek credentials are not configured.",
        retryable=False,
        requires_user_action=True,
        suggested_actions=("Configure DEEPSEEK_API_KEY.",),
    )
    assert error.to_dict() == {
        "code": "provider.missing_key",
        "category": "provider_contract",
        "message_for_user": "DeepSeek credentials are not configured.",
        "technical_detail_ref": None,
        "retryable": False,
        "requires_user_action": True,
        "scene_may_have_changed": False,
        "suggested_actions": ["Configure DEEPSEEK_API_KEY."],
        "cause_chain": [],
    }


def test_agent_exception_exposes_structured_error() -> None:
    error = AgentError(
        code="internal.invariant",
        category=ErrorCategory.INTERNAL_INVARIANT,
        message_for_user="An internal invariant failed.",
    )
    exc = AgentException(error)
    assert exc.error is error
    assert str(exc) == "An internal invariant failed."


def test_agent_error_requires_namespaced_code() -> None:
    with pytest.raises(ValueError, match="namespaced"):
        AgentError(
            code="bad",
            category=ErrorCategory.PROTOCOL,
            message_for_user="Bad code.",
        )
```

Create `tests/core/test_artifacts.py`:

```python
import pytest

from eee_agent.core.artifacts import ArtifactRef
from eee_agent.core.ids import IdKind, new_id


def test_artifact_ref_serializes_stable_fields() -> None:
    ref = ArtifactRef(
        artifact_id=new_id(IdKind.ARTIFACT),
        relative_path="runs/run_1/report.json",
        sha256="a" * 64,
        media_type="application/json",
        size_bytes=42,
    )
    data = ref.to_dict()
    assert data["schema_version"] == 1
    assert data["relative_path"] == "runs/run_1/report.json"
    assert data["sha256"] == "a" * 64


@pytest.mark.parametrize("path", ["/absolute/report.json", "../escape.json", r"runs\\bad.json"])
def test_artifact_ref_rejects_unsafe_relative_path(path: str) -> None:
    with pytest.raises(ValueError, match="artifact path"):
        ArtifactRef(
            artifact_id=new_id(IdKind.ARTIFACT),
            relative_path=path,
            sha256="b" * 64,
            media_type="application/json",
            size_bytes=1,
        )


def test_artifact_ref_rejects_invalid_hash() -> None:
    with pytest.raises(ValueError, match="sha256"):
        ArtifactRef(
            artifact_id=new_id(IdKind.ARTIFACT),
            relative_path="report.json",
            sha256="not-a-hash",
            media_type="application/json",
            size_bytes=1,
        )
```

- [ ] **Step 2: Run tests to verify imports fail**

Run:

```powershell
uv run --extra eval pytest tests/core/test_errors.py tests/core/test_artifacts.py -v
```

Expected: collection fails because the modules do not exist.

- [ ] **Step 3: Implement structured errors**

Create `eee_agent/core/errors.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCategory(StrEnum):
    VALIDATION = "validation"
    PERMISSION = "permission"
    STALE_SCENE = "stale_scene"
    PROVIDER_CONTRACT = "provider_contract"
    PROVIDER_TRANSIENT = "provider_transient"
    HOUDINI_COOK = "houdini_cook"
    HOUDINI_BRIDGE = "houdini_bridge"
    ARTIFACT = "artifact"
    PROTOCOL = "protocol"
    INTERNAL_INVARIANT = "internal_invariant"
    CRITICAL_RECOVERY = "critical_recovery"


@dataclass(frozen=True, slots=True)
class AgentError:
    code: str
    category: ErrorCategory
    message_for_user: str
    technical_detail_ref: str | None = None
    retryable: bool = False
    requires_user_action: bool = False
    scene_may_have_changed: bool = False
    suggested_actions: tuple[str, ...] = ()
    cause_chain: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if "." not in self.code or self.code.startswith(".") or self.code.endswith("."):
            raise ValueError("AgentError.code must be a namespaced value")
        if not self.message_for_user.strip():
            raise ValueError("AgentError.message_for_user must not be empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "category": self.category.value,
            "message_for_user": self.message_for_user,
            "technical_detail_ref": self.technical_detail_ref,
            "retryable": self.retryable,
            "requires_user_action": self.requires_user_action,
            "scene_may_have_changed": self.scene_may_have_changed,
            "suggested_actions": list(self.suggested_actions),
            "cause_chain": list(self.cause_chain),
        }


class AgentException(RuntimeError):
    def __init__(self, error: AgentError) -> None:
        self.error = error
        super().__init__(error.message_for_user)
```

- [ ] **Step 4: Implement immutable artifact references**

Create `eee_agent/core/artifacts.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from eee_agent.core.ids import IdKind, require_id


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    relative_path: str
    sha256: str
    media_type: str
    size_bytes: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_id(self.artifact_id, IdKind.ARTIFACT)
        path = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in self.relative_path
        ):
            raise ValueError(f"unsafe artifact path: {self.relative_path!r}")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("sha256 must contain exactly 64 lowercase hex characters")
        if not self.media_type.strip() or "/" not in self.media_type:
            raise ValueError("media_type must be a MIME type")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "schema_version": self.schema_version,
        }
```

Update `eee_agent/core/__init__.py`:

```python
from eee_agent.core.artifacts import ArtifactRef
from eee_agent.core.errors import AgentError, AgentException, ErrorCategory
from eee_agent.core.ids import IdKind, new_id, require_id

__all__ = [
    "AgentError",
    "AgentException",
    "ArtifactRef",
    "ErrorCategory",
    "IdKind",
    "new_id",
    "require_id",
]
```

- [ ] **Step 5: Run error and artifact tests**

Run:

```powershell
uv run --extra eval pytest tests/core/test_errors.py tests/core/test_artifacts.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit core errors and artifacts**

```powershell
git add eee_agent/core tests/core/test_errors.py tests/core/test_artifacts.py
git commit -m "feat: add structured errors and artifact refs"
```

### Task 5: Core Domain Event Envelope

**Files:**
- Create: `eee_agent/core/events.py`
- Modify: `eee_agent/core/__init__.py`
- Create: `tests/core/test_events.py`

- [ ] **Step 1: Write failing event tests**

Create `tests/core/test_events.py`:

```python
from datetime import datetime, timezone

import pytest

from eee_agent.core.events import DomainEvent


def test_domain_event_serializes_utc_timestamp_and_payload() -> None:
    event = DomainEvent.create(
        event_type="provider.model_completed",
        payload={"model": "deepseek-v4-pro", "tokens": 12},
        timestamp=datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc),
    )
    data = event.to_dict()
    assert data["event_type"] == "provider.model_completed"
    assert data["timestamp"] == "2026-07-13T04:00:00+00:00"
    assert data["payload"] == {"model": "deepseek-v4-pro", "tokens": 12}
    assert data["schema_version"] == 1


def test_domain_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DomainEvent.create(
            event_type="run.created",
            payload={},
            timestamp=datetime(2026, 7, 13, 4, 0),
        )


def test_domain_event_requires_namespaced_type() -> None:
    with pytest.raises(ValueError, match="namespaced"):
        DomainEvent.create(event_type="created", payload={})
```

- [ ] **Step 2: Run tests to verify the module is absent**

Run:

```powershell
uv run --extra eval pytest tests/core/test_events.py -v
```

Expected: collection fails because `eee_agent.core.events` is absent.

- [ ] **Step 3: Implement the event envelope**

Create `eee_agent/core/events.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypeAlias

from eee_agent.core.ids import IdKind, new_id, require_id


JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class DomainEvent:
    event_id: str
    event_type: str
    timestamp: datetime
    payload: dict[str, JsonValue]
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_id(self.event_id, IdKind.EVENT)
        if "." not in self.event_type:
            raise ValueError("event_type must be namespaced")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        payload: dict[str, JsonValue],
        timestamp: datetime | None = None,
    ) -> "DomainEvent":
        return cls(
            event_id=new_id(IdKind.EVENT),
            event_type=event_type,
            timestamp=timestamp or datetime.now(timezone.utc),
            payload=payload,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "payload": self.payload,
            "schema_version": self.schema_version,
        }
```

Add the export to `eee_agent/core/__init__.py`:

```python
from eee_agent.core.events import DomainEvent
```

Add `"DomainEvent"` to `__all__`.

- [ ] **Step 4: Run core event tests**

Run:

```powershell
uv run --extra eval pytest tests/core/test_events.py -v
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit the event contract**

```powershell
git add eee_agent/core tests/core/test_events.py
git commit -m "feat: add domain event envelope"
```

### Task 6: Provider Contracts And Registry

**Files:**
- Create: `eee_agent/providers/__init__.py`
- Create: `eee_agent/providers/contracts.py`
- Create: `eee_agent/providers/registry.py`
- Create: `eee_agent/providers/secrets.py`
- Create: `tests/providers/test_contracts.py`

- [ ] **Step 1: Write failing provider-contract tests**

Create `tests/providers/test_contracts.py`:

```python
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from eee_agent.providers.contracts import (
    ModelCapabilities,
    ModelProfile,
    ModelRole,
    ModelVerification,
    ProviderConnection,
    ProviderKind,
    RoleBindings,
    ThinkingEffort,
    Transport,
    VerificationCheck,
    VerificationStatus,
)
from eee_agent.providers.registry import ProviderRegistry


class FakeAdapter:
    kind = ProviderKind.DEEPSEEK

    def build(self, connection: ProviderConnection, profile: ModelProfile):
        return FakeListChatModel(responses=[profile.model_name])


def connection() -> ProviderConnection:
    return ProviderConnection(
        connection_id="deepseek-main",
        provider=ProviderKind.DEEPSEEK,
        transport=Transport.ANTHROPIC,
        base_url="https://api.deepseek.com/anthropic",
        secret_ref="env:DEEPSEEK_API_KEY",
    )


def profile() -> ModelProfile:
    return ModelProfile(
        profile_id="primary",
        connection_id="deepseek-main",
        model_name="deepseek-v4-pro",
        capabilities=ModelCapabilities(
            streaming=True,
            thinking=True,
            tool_calling=True,
            structured_output=True,
            image_input=False,
        ),
        thinking_enabled=True,
        effort=ThinkingEffort.MAX,
        max_output_tokens=8192,
    )


def test_registry_resolves_adapter_and_frozen_snapshot() -> None:
    registry = ProviderRegistry()
    registry.register(FakeAdapter())
    resolved = registry.resolve(connection(), profile())
    assert resolved.connection == connection()
    assert resolved.profile == profile()
    assert isinstance(resolved.model, FakeListChatModel)


def test_registry_rejects_duplicate_adapter() -> None:
    registry = ProviderRegistry()
    registry.register(FakeAdapter())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeAdapter())


def test_registry_rejects_profile_connection_mismatch() -> None:
    registry = ProviderRegistry()
    registry.register(FakeAdapter())
    wrong = ModelProfile(
        profile_id="wrong",
        connection_id="another-connection",
        model_name="deepseek-v4-pro",
        capabilities=profile().capabilities,
    )
    with pytest.raises(ValueError, match="connection_id"):
        registry.resolve(connection(), wrong)


def test_role_bindings_and_verification_are_provider_neutral() -> None:
    bindings = RoleBindings(primary_profile_id="primary", vision_profile_id=None)
    verification = ModelVerification(
        status=VerificationStatus.VERIFIED,
        requested_model_name="deepseek-v4-pro",
        actual_model_name="deepseek-v4-pro",
        checks=(VerificationCheck(name="tool_replay", passed=True, detail=None),),
    )
    assert bindings.profile_for(ModelRole.PRIMARY) == "primary"
    assert bindings.profile_for(ModelRole.VISION) is None
    assert verification.status is VerificationStatus.VERIFIED
```

- [ ] **Step 2: Run tests to verify provider modules are absent**

Run:

```powershell
uv run --extra eval pytest tests/providers/test_contracts.py -v
```

Expected: collection fails with `ModuleNotFoundError: eee_agent.providers`.

- [ ] **Step 3: Implement provider contracts**

Create `eee_agent/providers/contracts.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from langchain_core.language_models import BaseChatModel


class ProviderKind(StrEnum):
    DEEPSEEK = "deepseek"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class Transport(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class ThinkingEffort(StrEnum):
    HIGH = "high"
    MAX = "max"


class ModelRole(StrEnum):
    PRIMARY = "primary"
    VISION = "vision"


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    streaming: bool
    thinking: bool
    tool_calling: bool
    structured_output: bool
    image_input: bool


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    connection_id: str
    provider: ProviderKind
    transport: Transport
    base_url: str | None
    secret_ref: str
    timeout_seconds: float = 120.0
    max_retries: int = 2

    def __post_init__(self) -> None:
        if not self.connection_id.strip():
            raise ValueError("connection_id must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")


@dataclass(frozen=True, slots=True)
class ModelProfile:
    profile_id: str
    connection_id: str
    model_name: str
    capabilities: ModelCapabilities
    thinking_enabled: bool = False
    effort: ThinkingEffort | None = None
    max_output_tokens: int = 8192

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.model_name.strip():
            raise ValueError("profile_id and model_name must not be empty")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.thinking_enabled and not self.capabilities.thinking:
            raise ValueError("thinking cannot be enabled for a non-thinking profile")


@dataclass(frozen=True, slots=True)
class RoleBindings:
    primary_profile_id: str
    vision_profile_id: str | None = None

    def __post_init__(self) -> None:
        if not self.primary_profile_id.strip():
            raise ValueError("primary_profile_id must not be empty")

    def profile_for(self, role: ModelRole) -> str | None:
        if role is ModelRole.PRIMARY:
            return self.primary_profile_id
        return self.vision_profile_id


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    name: str
    passed: bool
    detail: str | None


@dataclass(frozen=True, slots=True)
class ModelVerification:
    status: VerificationStatus
    requested_model_name: str
    actual_model_name: str | None
    checks: tuple[VerificationCheck, ...]


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    connection: ProviderConnection
    profile: ModelProfile
    model: BaseChatModel


class ProviderAdapter(Protocol):
    kind: ProviderKind

    def build(
        self,
        connection: ProviderConnection,
        profile: ModelProfile,
    ) -> BaseChatModel: ...
```

- [ ] **Step 4: Implement the registry and secret resolver**

Create `eee_agent/providers/registry.py`:

```python
from __future__ import annotations

from eee_agent.providers.contracts import (
    ModelProfile,
    ProviderAdapter,
    ProviderConnection,
    ProviderKind,
    ResolvedModel,
)


class ProviderRegistry:
    def __init__(self) -> None:
        self._adapters: dict[ProviderKind, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> None:
        if adapter.kind in self._adapters:
            raise ValueError(f"provider adapter already registered: {adapter.kind.value}")
        self._adapters[adapter.kind] = adapter

    def resolve(
        self,
        connection: ProviderConnection,
        profile: ModelProfile,
    ) -> ResolvedModel:
        if profile.connection_id != connection.connection_id:
            raise ValueError(
                "profile connection_id does not match the selected connection"
            )
        try:
            adapter = self._adapters[connection.provider]
        except KeyError as exc:
            raise ValueError(
                f"no provider adapter registered for {connection.provider.value}"
            ) from exc
        model = adapter.build(connection, profile)
        return ResolvedModel(connection=connection, profile=profile, model=model)
```

Create `eee_agent/providers/secrets.py`:

```python
from __future__ import annotations

import os

from eee_agent.core.errors import AgentError, AgentException, ErrorCategory


def resolve_secret(secret_ref: str) -> str:
    prefix = "env:"
    if not secret_ref.startswith(prefix):
        raise AgentException(
            AgentError(
                code="provider.unsupported_secret_ref",
                category=ErrorCategory.PROVIDER_CONTRACT,
                message_for_user="This secret source is not supported by Foundation.",
            )
        )
    variable = secret_ref[len(prefix):]
    value = os.getenv(variable)
    if value:
        return value
    raise AgentException(
        AgentError(
            code="provider.missing_key",
            category=ErrorCategory.PROVIDER_CONTRACT,
            message_for_user=f"Required credential {variable} is not configured.",
            requires_user_action=True,
            suggested_actions=(f"Configure {variable} for the next run.",),
        )
    )
```

Create `eee_agent/providers/__init__.py`:

```python
from eee_agent.providers.contracts import (
    ModelCapabilities,
    ModelProfile,
    ModelRole,
    ModelVerification,
    ProviderConnection,
    ProviderKind,
    ResolvedModel,
    RoleBindings,
    ThinkingEffort,
    Transport,
    VerificationCheck,
    VerificationStatus,
)
from eee_agent.providers.registry import ProviderRegistry

__all__ = [
    "ModelCapabilities",
    "ModelProfile",
    "ModelRole",
    "ModelVerification",
    "ProviderConnection",
    "ProviderKind",
    "ProviderRegistry",
    "ResolvedModel",
    "RoleBindings",
    "ThinkingEffort",
    "Transport",
    "VerificationCheck",
    "VerificationStatus",
]
```

- [ ] **Step 5: Run provider contract tests**

Run:

```powershell
uv run --extra eval pytest tests/providers/test_contracts.py -v
```

Expected: 4 tests pass.

- [ ] **Step 6: Commit provider contracts**

```powershell
git add eee_agent/providers tests/providers/test_contracts.py
git commit -m "feat: add provider contracts and registry"
```

### Task 7: DeepSeek V4 Anthropic Adapter And Payload Contract

**Files:**
- Create: `eee_agent/providers/deepseek_v4.py`
- Create: `tests/providers/test_deepseek_v4.py`

- [ ] **Step 1: Write failing adapter and payload tests**

Create `tests/providers/test_deepseek_v4.py`:

```python
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from eee_agent.core.errors import AgentException
from eee_agent.providers.contracts import (
    ModelCapabilities,
    ModelProfile,
    ProviderConnection,
    ProviderKind,
    ThinkingEffort,
    Transport,
)
from eee_agent.providers.deepseek_v4 import (
    DEEPSEEK_ANTHROPIC_URL,
    DeepSeekV4ProviderAdapter,
)


def connection() -> ProviderConnection:
    return ProviderConnection(
        connection_id="deepseek-official",
        provider=ProviderKind.DEEPSEEK,
        transport=Transport.ANTHROPIC,
        base_url=DEEPSEEK_ANTHROPIC_URL,
        secret_ref="env:DEEPSEEK_API_KEY",
    )


def profile(model_name: str = "deepseek-v4-pro") -> ModelProfile:
    return ModelProfile(
        profile_id="primary",
        connection_id="deepseek-official",
        model_name=model_name,
        capabilities=ModelCapabilities(True, True, True, True, False),
        thinking_enabled=True,
        effort=ThinkingEffort.MAX,
        max_output_tokens=8192,
    )


def test_adapter_builds_chat_anthropic_with_official_v4_settings(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    model = DeepSeekV4ProviderAdapter().build(connection(), profile())
    assert isinstance(model, ChatAnthropic)
    assert model.model == "deepseek-v4-pro"
    assert model.anthropic_api_url == DEEPSEEK_ANTHROPIC_URL
    assert model.thinking == {"type": "enabled"}
    assert model.effort == "max"
    assert model.output_version == "v1"


@pytest.mark.parametrize("alias", ["deepseek-chat", "deepseek-reasoner", "claude-sonnet-5"])
def test_adapter_rejects_aliases_that_could_silently_map_to_flash(
    monkeypatch, alias: str
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    with pytest.raises(AgentException) as caught:
        DeepSeekV4ProviderAdapter().build(connection(), profile(alias))
    assert caught.value.error.code == "provider.invalid_model"


def test_adapter_reports_missing_key_without_leaking_a_value(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(AgentException) as caught:
        DeepSeekV4ProviderAdapter().build(connection(), profile())
    assert caught.value.error.code == "provider.missing_key"


def test_anthropic_payload_replays_thinking_before_tool_result(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    model = DeepSeekV4ProviderAdapter().build(connection(), profile())
    assistant = AIMessage(
        content=[
            {"type": "thinking", "thinking": "Need the lookup tool.", "signature": "sig"},
            {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
        ]
    )
    payload = model._get_request_payload(  # noqa: SLF001 - intentional adapter contract
        [
            HumanMessage(content="Find x"),
            assistant,
            ToolMessage(content="result", tool_call_id="toolu_1"),
        ]
    )
    assistant_content = payload["messages"][1]["content"]
    assert assistant_content[0] == {
        "type": "thinking",
        "thinking": "Need the lookup tool.",
        "signature": "sig",
    }
    assert assistant_content[1]["type"] == "tool_use"
    assert payload["messages"][2]["content"][0]["type"] == "tool_result"
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["output_config"] == {"effort": "max"}
```

- [ ] **Step 2: Run tests to verify the adapter is absent**

Run:

```powershell
uv run --extra eval pytest tests/providers/test_deepseek_v4.py -v
```

Expected: collection fails because `eee_agent.providers.deepseek_v4` is absent.

- [ ] **Step 3: Implement the strict DeepSeek adapter**

Create `eee_agent/providers/deepseek_v4.py`:

```python
from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel

from eee_agent.core.errors import AgentError, AgentException, ErrorCategory
from eee_agent.providers.contracts import (
    ModelProfile,
    ProviderConnection,
    ProviderKind,
    Transport,
)
from eee_agent.providers.secrets import resolve_secret


DEEPSEEK_ANTHROPIC_URL = "https://api.deepseek.com/anthropic"
SUPPORTED_DEEPSEEK_MODELS = frozenset({"deepseek-v4-pro", "deepseek-v4-flash"})


class DeepSeekV4ProviderAdapter:
    kind = ProviderKind.DEEPSEEK

    def build(
        self,
        connection: ProviderConnection,
        profile: ModelProfile,
    ) -> BaseChatModel:
        if connection.transport is not Transport.ANTHROPIC:
            raise self._configuration_error(
                "provider.invalid_transport",
                "DeepSeek V4 must use the Anthropic transport.",
            )
        if (connection.base_url or "").rstrip("/") != DEEPSEEK_ANTHROPIC_URL:
            raise self._configuration_error(
                "provider.invalid_endpoint",
                "DeepSeek V4 must use the official Anthropic endpoint.",
            )
        if profile.model_name not in SUPPORTED_DEEPSEEK_MODELS:
            raise self._configuration_error(
                "provider.invalid_model",
                "Use an exact DeepSeek V4 model name; aliases are not accepted.",
            )
        if profile.capabilities.image_input:
            raise self._configuration_error(
                "provider.invalid_capability",
                "DeepSeek V4 Anthropic transport does not support image input.",
            )
        api_key = resolve_secret(connection.secret_ref)
        thinking = {"type": "enabled" if profile.thinking_enabled else "disabled"}
        effort = profile.effort.value if profile.thinking_enabled and profile.effort else None
        return ChatAnthropic(
            model=profile.model_name,
            base_url=DEEPSEEK_ANTHROPIC_URL,
            api_key=api_key,
            max_tokens=profile.max_output_tokens,
            thinking=thinking,
            effort=effort,
            output_version="v1",
            timeout=connection.timeout_seconds,
            max_retries=connection.max_retries,
            stream_usage=True,
        )

    @staticmethod
    def _configuration_error(code: str, message: str) -> AgentException:
        return AgentException(
            AgentError(
                code=code,
                category=ErrorCategory.PROVIDER_CONTRACT,
                message_for_user=message,
                requires_user_action=True,
            )
        )
```

- [ ] **Step 4: Run DeepSeek adapter tests**

Run:

```powershell
uv run --extra eval pytest tests/providers/test_deepseek_v4.py -v
```

Expected: all adapter and tool-replay payload tests pass without network access.

- [ ] **Step 5: Commit the DeepSeek adapter**

```powershell
git add eee_agent/providers/deepseek_v4.py tests/providers/test_deepseek_v4.py
git commit -m "feat: add DeepSeek V4 Anthropic adapter"
```

### Task 8: Standard Adapters And Provider-Neutral Model Factory

**Files:**
- Create: `eee_agent/providers/anthropic.py`
- Create: `eee_agent/providers/openai.py`
- Create: `eee_agent/providers/factory.py`
- Modify: `eee_agent/config.py`
- Modify: `eee_agent/model.py`
- Create: `tests/providers/test_factory.py`

- [ ] **Step 1: Write failing factory tests**

Create `tests/providers/test_factory.py`:

```python
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
import pytest

from eee_agent.config import llm_config
from eee_agent.core.errors import AgentException
from eee_agent.model import build_model


def clear_model_env(monkeypatch) -> None:
    for name in (
        "EEE_LLM_PROVIDER",
        "EEE_LLM_MODEL",
        "EEE_LLM_THINKING",
        "EEE_LLM_EFFORT",
        "EEE_LLM_MAX_TOKENS",
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_config_selects_strict_deepseek_v4(monkeypatch) -> None:
    clear_model_env(monkeypatch)
    config = llm_config()
    assert config.provider == "deepseek"
    assert config.model == "deepseek-v4-pro"
    assert config.thinking_enabled is True
    assert config.effort == "max"
    assert config.max_output_tokens == 8192


def test_build_model_uses_chat_anthropic_for_deepseek(monkeypatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    model = build_model()
    assert isinstance(model, ChatAnthropic)
    assert model.anthropic_api_url == "https://api.deepseek.com/anthropic"


def test_build_model_preserves_standard_anthropic(monkeypatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("EEE_LLM_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unit-test-key")
    assert isinstance(build_model(), ChatAnthropic)


def test_build_model_preserves_standard_openai(monkeypatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("EEE_LLM_MODEL", "gpt-4.1")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-key")
    assert isinstance(build_model(), ChatOpenAI)


def test_deepseek_alias_is_rejected_before_any_request(monkeypatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    monkeypatch.setenv("EEE_LLM_MODEL", "deepseek-reasoner")
    with pytest.raises(AgentException) as caught:
        build_model()
    assert caught.value.error.code == "provider.invalid_model"
```

- [ ] **Step 2: Run tests to verify current DeepSeek construction fails expectations**

Run:

```powershell
uv run --extra eval pytest tests/providers/test_factory.py -v
```

Expected: failures show the current model is `ChatOpenAI` and `LlmConfig` lacks thinking fields.

- [ ] **Step 3: Extend validated LLM configuration**

Update the `LlmConfig` declaration and `llm_config()` in `eee_agent/config.py`:

```python
@dataclass(frozen=True)
class LlmConfig:
    provider: str
    model: str
    thinking_enabled: bool
    effort: str | None
    max_output_tokens: int


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    raise ValueError(f"{name} must be enabled or disabled")


def llm_config() -> LlmConfig:
    provider = os.getenv("EEE_LLM_PROVIDER", "deepseek").strip().lower()
    defaults = {
        "deepseek": "deepseek-v4-pro",
        "anthropic": "claude-sonnet-5",
        "openai": "gpt-4.1",
    }
    if provider not in defaults:
        raise ValueError(f"unknown EEE_LLM_PROVIDER: {provider!r}")
    model = os.getenv("EEE_LLM_MODEL") or defaults[provider]
    thinking_enabled = _env_bool("EEE_LLM_THINKING", provider == "deepseek")
    effort = os.getenv("EEE_LLM_EFFORT") or ("max" if provider == "deepseek" else None)
    if effort not in {None, "high", "max"}:
        raise ValueError("EEE_LLM_EFFORT must be high or max")
    max_output_tokens = int(os.getenv("EEE_LLM_MAX_TOKENS", "8192"))
    if max_output_tokens <= 0:
        raise ValueError("EEE_LLM_MAX_TOKENS must be positive")
    return LlmConfig(
        provider=provider,
        model=model,
        thinking_enabled=thinking_enabled,
        effort=effort,
        max_output_tokens=max_output_tokens,
    )
```

- [ ] **Step 4: Implement standard provider adapters**

Create `eee_agent/providers/anthropic.py`:

```python
from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel

from eee_agent.providers.contracts import (
    ModelProfile,
    ProviderConnection,
    ProviderKind,
    Transport,
)
from eee_agent.providers.secrets import resolve_secret


class AnthropicProviderAdapter:
    kind = ProviderKind.ANTHROPIC

    def build(
        self,
        connection: ProviderConnection,
        profile: ModelProfile,
    ) -> BaseChatModel:
        if connection.transport is not Transport.ANTHROPIC:
            raise ValueError("AnthropicProviderAdapter requires Anthropic transport")
        kwargs: dict[str, object] = {
            "model": profile.model_name,
            "api_key": resolve_secret(connection.secret_ref),
            "max_tokens": profile.max_output_tokens,
            "timeout": connection.timeout_seconds,
            "max_retries": connection.max_retries,
            "output_version": "v1",
            "stream_usage": True,
        }
        if connection.base_url:
            kwargs["base_url"] = connection.base_url
        if profile.thinking_enabled:
            kwargs["thinking"] = {"type": "enabled"}
            if profile.effort:
                kwargs["effort"] = profile.effort.value
        return ChatAnthropic(**kwargs)
```

Create `eee_agent/providers/openai.py`:

```python
from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from eee_agent.providers.contracts import (
    ModelProfile,
    ProviderConnection,
    ProviderKind,
    Transport,
)
from eee_agent.providers.secrets import resolve_secret


class OpenAIProviderAdapter:
    kind = ProviderKind.OPENAI

    def build(
        self,
        connection: ProviderConnection,
        profile: ModelProfile,
    ) -> BaseChatModel:
        if connection.transport is not Transport.OPENAI:
            raise ValueError("OpenAIProviderAdapter requires OpenAI transport")
        kwargs: dict[str, object] = {
            "model": profile.model_name,
            "api_key": resolve_secret(connection.secret_ref),
            "timeout": connection.timeout_seconds,
            "max_retries": connection.max_retries,
            "stream_usage": True,
        }
        if connection.base_url:
            kwargs["base_url"] = connection.base_url
        return ChatOpenAI(**kwargs)
```

- [ ] **Step 5: Implement environment-to-registry resolution**

Create `eee_agent/providers/factory.py`:

```python
from __future__ import annotations

from eee_agent.config import LlmConfig
from eee_agent.providers.anthropic import AnthropicProviderAdapter
from eee_agent.providers.contracts import (
    ModelCapabilities,
    ModelProfile,
    ProviderConnection,
    ProviderKind,
    ResolvedModel,
    ThinkingEffort,
    Transport,
)
from eee_agent.providers.deepseek_v4 import (
    DEEPSEEK_ANTHROPIC_URL,
    DeepSeekV4ProviderAdapter,
)
from eee_agent.providers.openai import OpenAIProviderAdapter
from eee_agent.providers.registry import ProviderRegistry


def build_default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(DeepSeekV4ProviderAdapter())
    registry.register(AnthropicProviderAdapter())
    registry.register(OpenAIProviderAdapter())
    return registry


def resolve_model(config: LlmConfig) -> ResolvedModel:
    provider = ProviderKind(config.provider)
    connection_id = f"{provider.value}-environment"
    if provider is ProviderKind.DEEPSEEK:
        transport = Transport.ANTHROPIC
        base_url = DEEPSEEK_ANTHROPIC_URL
        secret_ref = "env:DEEPSEEK_API_KEY"
        capabilities = ModelCapabilities(True, True, True, True, False)
    elif provider is ProviderKind.ANTHROPIC:
        transport = Transport.ANTHROPIC
        base_url = None
        secret_ref = "env:ANTHROPIC_API_KEY"
        capabilities = ModelCapabilities(True, True, True, True, False)
    else:
        transport = Transport.OPENAI
        base_url = None
        secret_ref = "env:OPENAI_API_KEY"
        capabilities = ModelCapabilities(True, False, True, True, False)

    effort = ThinkingEffort(config.effort) if config.effort else None
    connection = ProviderConnection(
        connection_id=connection_id,
        provider=provider,
        transport=transport,
        base_url=base_url,
        secret_ref=secret_ref,
    )
    profile = ModelProfile(
        profile_id="primary",
        connection_id=connection_id,
        model_name=config.model,
        capabilities=capabilities,
        thinking_enabled=config.thinking_enabled,
        effort=effort,
        max_output_tokens=config.max_output_tokens,
    )
    return build_default_registry().resolve(connection, profile)
```

- [ ] **Step 6: Replace the direct model branches**

Replace `eee_agent/model.py` with:

```python
"""Provider-neutral model factory."""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from eee_agent.config import llm_config
from eee_agent.providers.factory import resolve_model


def build_model() -> BaseChatModel:
    return resolve_model(llm_config()).model
```

- [ ] **Step 7: Run factory and provider tests**

Run:

```powershell
uv run --extra eval pytest tests/providers/test_factory.py tests/providers/test_deepseek_v4.py -v
```

Expected: all tests pass; no network requests occur.

- [ ] **Step 8: Commit provider-neutral model construction**

```powershell
git add eee_agent/config.py eee_agent/model.py eee_agent/providers tests/providers/test_factory.py
git commit -m "refactor: route models through provider registry"
```

### Task 9: Normalized Provider Events

**Files:**
- Create: `eee_agent/providers/events.py`
- Create: `eee_agent/providers/normalize.py`
- Create: `tests/providers/test_events.py`
- Modify: `eee_agent/cli.py`
- Create: `tests/test_cli_stream_events.py`

- [ ] **Step 1: Write failing normalization tests**

Create `tests/providers/test_events.py`:

```python
from langchain_core.messages import AIMessageChunk

from eee_agent.providers.events import (
    ReasoningDelta,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallStarted,
    UsageUpdated,
)
from eee_agent.providers.normalize import normalize_message_chunk


def test_normalize_standard_reasoning_text_tool_and_usage() -> None:
    chunk = AIMessageChunk(
        content=[
            {"type": "reasoning", "reasoning": "Inspect the component graph."},
            {"type": "text", "text": "I will inspect it."},
        ],
        tool_call_chunks=[
            {
                "name": "inspect_graph",
                "args": '{"path":',
                "id": "call_1",
                "index": 0,
                "type": "tool_call_chunk",
            }
        ],
        usage_metadata={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
    )
    events = normalize_message_chunk(chunk)
    assert ReasoningDelta("Inspect the component graph.") in events
    assert TextDelta("I will inspect it.") in events
    assert ToolCallStarted("call_1", "inspect_graph", 0) in events
    assert ToolCallArgumentsDelta("call_1", '{"path":', 0) in events
    assert UsageUpdated(3, 5, 8) in events


def test_normalize_legacy_reasoning_content_during_migration() -> None:
    chunk = AIMessageChunk(
        content="answer",
        additional_kwargs={"reasoning_content": "legacy thought"},
    )
    assert normalize_message_chunk(chunk) == (
        ReasoningDelta("legacy thought"),
        TextDelta("answer"),
    )
```

Create `tests/test_cli_stream_events.py`:

```python
from langchain_core.messages import AIMessageChunk

from eee_agent.cli import _legacy_stream_events


def test_legacy_stdio_mapping_keeps_reasoning_separate_from_text() -> None:
    chunk = AIMessageChunk(
        content=[
            {"type": "reasoning", "reasoning": "Check the graph."},
            {"type": "text", "text": "The graph is valid."},
        ],
        tool_call_chunks=[
            {
                "name": "inspect_graph",
                "args": "{}",
                "id": "call_1",
                "index": 0,
                "type": "tool_call_chunk",
            }
        ],
        usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
    )
    events = _legacy_stream_events(chunk)
    assert [event["type"] for event in events] == [
        "thinking",
        "token",
        "tool_call",
        "tool_call_args",
        "usage",
    ]
    assert events[0]["text"] == "Check the graph."
    assert events[1]["text"] == "The graph is valid."
```

- [ ] **Step 2: Run tests to verify event modules are absent**

Run:

```powershell
uv run --extra eval pytest tests/providers/test_events.py -v
```

Expected: collection fails because normalized event modules are absent.

- [ ] **Step 3: Define normalized event types**

Create `eee_agent/providers/events.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from eee_agent.core.errors import AgentError


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    call_id: str
    name: str
    index: int


@dataclass(frozen=True, slots=True)
class ToolCallArgumentsDelta:
    call_id: str
    arguments_delta: str
    index: int


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    call_id: str
    name: str


@dataclass(frozen=True, slots=True)
class UsageUpdated:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class ModelCompleted:
    model_name: str | None


@dataclass(frozen=True, slots=True)
class ModelFailed:
    error: AgentError


ProviderEvent: TypeAlias = (
    ReasoningDelta
    | TextDelta
    | ToolCallStarted
    | ToolCallArgumentsDelta
    | ToolCallCompleted
    | UsageUpdated
    | ModelCompleted
    | ModelFailed
)
```

- [ ] **Step 4: Implement LangChain chunk normalization**

Create `eee_agent/providers/normalize.py`:

```python
from __future__ import annotations

import json
from collections.abc import Mapping

from langchain_core.messages import AIMessageChunk

from eee_agent.providers.events import (
    ProviderEvent,
    ReasoningDelta,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallStarted,
    UsageUpdated,
)


def normalize_message_chunk(chunk: AIMessageChunk) -> tuple[ProviderEvent, ...]:
    events: list[ProviderEvent] = []
    additional = chunk.additional_kwargs or {}
    legacy_reasoning = additional.get("reasoning_content")
    if legacy_reasoning:
        events.append(ReasoningDelta(str(legacy_reasoning)))

    if isinstance(chunk.content, str):
        if chunk.content:
            events.append(TextDelta(chunk.content))
    else:
        for block in chunk.content:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type in {"thinking", "reasoning"}:
                text = block.get("thinking") or block.get("reasoning") or block.get("text")
                if text:
                    events.append(ReasoningDelta(str(text)))
            elif block_type == "text" and block.get("text"):
                events.append(TextDelta(str(block["text"])))

    for tool_chunk in chunk.tool_call_chunks or []:
        call_id = str(tool_chunk.get("id") or "")
        index = int(tool_chunk.get("index") or 0)
        name = str(tool_chunk.get("name") or "")
        if name:
            events.append(ToolCallStarted(call_id, name, index))
        arguments = tool_chunk.get("args")
        if isinstance(arguments, Mapping):
            arguments = json.dumps(arguments, separators=(",", ":"))
        if arguments:
            events.append(ToolCallArgumentsDelta(call_id, str(arguments), index))

    usage = chunk.usage_metadata
    if usage:
        events.append(
            UsageUpdated(
                int(usage.get("input_tokens", 0)),
                int(usage.get("output_tokens", 0)),
                int(usage.get("total_tokens", 0)),
            )
        )
    return tuple(events)
```

- [ ] **Step 5: Map normalized events onto the current stdio protocol**

Add this compatibility adapter above `_run_turn()` in `eee_agent/cli.py`:

```python
def _legacy_stream_events(chunk) -> list[dict[str, object]]:
    """Map provider-neutral events onto the current JSON-lines protocol."""
    from eee_agent.providers.events import (
        ReasoningDelta,
        TextDelta,
        ToolCallArgumentsDelta,
        ToolCallStarted,
        UsageUpdated,
    )
    from eee_agent.providers.normalize import normalize_message_chunk

    legacy: list[dict[str, object]] = []
    for event in normalize_message_chunk(chunk):
        if isinstance(event, ReasoningDelta):
            legacy.append({"type": "thinking", "text": event.text})
        elif isinstance(event, TextDelta):
            legacy.append({"type": "token", "text": event.text})
        elif isinstance(event, ToolCallStarted):
            legacy.append(
                {
                    "type": "tool_call",
                    "id": event.call_id,
                    "name": event.name,
                    "index": event.index,
                    "args": "",
                }
            )
        elif isinstance(event, ToolCallArgumentsDelta):
            legacy.append(
                {
                    "type": "tool_call_args",
                    "id": event.call_id,
                    "index": event.index,
                    "args": event.arguments_delta,
                }
            )
        elif isinstance(event, UsageUpdated):
            legacy.append(
                {
                    "type": "usage",
                    "tokens_in": event.input_tokens,
                    "tokens_out": event.output_tokens,
                    "tokens_total": event.total_tokens,
                }
            )
    return legacy
```

Inside the `AIMessageChunk` branch of `_run_turn()`, replace the direct
`additional_kwargs["reasoning_content"]`, content, tool chunk, and usage parsing
with:

```python
                    for legacy in _legacy_stream_events(chunk):
                        legacy_type = legacy["type"]
                        if legacy_type == "usage":
                            tok_in += int(legacy["tokens_in"])
                            tok_out += int(legacy["tokens_out"])
                            continue
                        _emit(legacy)
                        if legacy_type == "tool_call":
                            metric()
```

- [ ] **Step 6: Run normalized and legacy compatibility tests**

Run:

```powershell
uv run --extra eval pytest tests/providers/test_events.py tests/test_cli_stream_events.py -v
```

Expected: 3 tests pass; reasoning and text remain separate in the old Panel protocol.

- [ ] **Step 7: Commit normalized provider events**

```powershell
git add eee_agent/providers/events.py eee_agent/providers/normalize.py eee_agent/cli.py tests/providers/test_events.py tests/test_cli_stream_events.py
git commit -m "feat: normalize provider stream events"
```

### Task 10: Make Deep Agents Harness Behavior Explicit

**Files:**
- Create: `eee_agent/harness.py`
- Modify: `eee_agent/app.py`
- Create: `tests/test_harness.py`

- [ ] **Step 1: Write the failing hidden-subagent contract test**

Create `tests/test_harness.py`:

```python
from eee_agent.app import build_agent


def test_build_agent_has_no_implicit_general_purpose_subagent(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    monkeypatch.setenv("EEE_COMPACT_TOOL", "false")
    graph = build_agent()
    # Deep Agents has no public tool-introspection API. This deliberately checks
    # the compiled ToolNode so a dependency upgrade fails loudly if the task tool
    # or its inherited Houdini tools return.
    tool_names = set(graph.nodes["tools"].bound._tools_by_name)  # noqa: SLF001
    assert "task" not in tool_names
    assert "create_node" in tool_names
```

- [ ] **Step 2: Run the test to demonstrate current hidden behavior**

Run:

```powershell
uv run --extra eval pytest tests/test_harness.py -v
```

Expected: `FAIL` because Deep Agents 0.6.12 auto-adds `task` backed by `general-purpose`.

- [ ] **Step 3: Register an explicit no-general-purpose harness profile**

Create `eee_agent/harness.py`:

```python
from __future__ import annotations

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)


_configured = False


def configure_deepagents_harness() -> None:
    global _configured
    if _configured:
        return
    profile = HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)
    )
    # DeepSeekV4ProviderAdapter is implemented with ChatAnthropic, so Deep
    # Agents resolves its harness provider as "anthropic". Standard OpenAI is
    # registered separately. Both are process-local EEE Agent policies.
    register_harness_profile("anthropic", profile)
    register_harness_profile("openai", profile)
    _configured = True
```

- [ ] **Step 4: Configure the harness before graph creation**

In `eee_agent/app.py`, import the helper:

```python
from eee_agent.harness import configure_deepagents_harness
```

Call it immediately before `create_deep_agent(**kwargs)`:

```python
    configure_deepagents_harness()
    return create_deep_agent(**kwargs)
```

Replace the module docstring's claim that the implicit subagent behavior is acceptable with:

```python
"""Assemble the Deep Agents Houdini agent.

Foundation keeps the existing tool surface for compatibility but explicitly
disables Deep Agents' auto-added general-purpose subagent. Capability-specific
subagents will be registered later with bounded tools and structured outputs.
"""
```

- [ ] **Step 5: Run harness and graph-construction tests**

Run:

```powershell
uv run --extra eval pytest tests/test_harness.py -v
$env:DEEPSEEK_API_KEY = "graph-build-placeholder-not-used"
uv run python -c "from eee_agent.app import build_agent; print(type(build_agent()).__name__)"
Remove-Item Env:\DEEPSEEK_API_KEY
```

Expected: test passes and the smoke command prints `CompiledStateGraph` without a network request.

- [ ] **Step 6: Commit explicit harness behavior**

```powershell
git add eee_agent/app.py eee_agent/harness.py tests/test_harness.py
git commit -m "fix: disable implicit Deep Agents subagent"
```

### Task 11: Runtime Version Report And CLI Command

**Files:**
- Create: `eee_agent/core/versioning.py`
- Modify: `eee_agent/core/__init__.py`
- Modify: `eee_agent/cli.py`
- Create: `tests/core/test_versioning.py`
- Create: `tests/test_cli_versions.py`

- [ ] **Step 1: Write failing version-report tests**

Create `tests/core/test_versioning.py`:

```python
from eee_agent.core.versioning import runtime_version_report


def test_runtime_version_report_contains_reproducibility_fields() -> None:
    report = runtime_version_report()
    assert report["eee_agent"] == "0.1.0"
    assert report["python"].startswith("3.11.")
    assert report["dependencies"]["deepagents"] == "0.6.12"
    assert report["dependencies"]["rpyc"] == "4.1.0"
```

Create `tests/test_cli_versions.py`:

```python
import json

from eee_agent.cli import print_versions


def test_print_versions_emits_json(capsys) -> None:
    assert print_versions() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dependencies"]["langchain-anthropic"] == "1.4.8"
```

- [ ] **Step 2: Run tests to verify versioning is absent**

Run:

```powershell
uv run --extra eval pytest tests/core/test_versioning.py tests/test_cli_versions.py -v
```

Expected: collection fails because `eee_agent.core.versioning` and `print_versions` do not exist.

- [ ] **Step 3: Implement the version report**

Create `eee_agent/core/versioning.py`:

```python
from __future__ import annotations

import platform
import sys
from importlib import metadata

from eee_agent import __version__


TRACKED_DISTRIBUTIONS = (
    "deepagents",
    "langchain",
    "langchain-core",
    "langchain-openai",
    "langchain-anthropic",
    "langgraph",
    "langsmith",
    "openai",
    "anthropic",
    "rpyc",
)


def runtime_version_report() -> dict[str, object]:
    dependencies: dict[str, str | None] = {}
    for name in TRACKED_DISTRIBUTIONS:
        try:
            dependencies[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            dependencies[name] = None
    return {
        "eee_agent": __version__,
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "dependencies": dependencies,
    }
```

Add this import to `eee_agent/core/__init__.py` and add the name to `__all__`:

```python
from eee_agent.core.versioning import runtime_version_report
```

- [ ] **Step 4: Add the CLI command**

In `eee_agent/cli.py`, add:

```python
def print_versions() -> int:
    from eee_agent.core.versioning import runtime_version_report

    print(json.dumps(runtime_version_report(), indent=2, sort_keys=True))
    return 0
```

Add the parser:

```python
    sub.add_parser("versions", help="print runtime and dependency versions")
```

Add the dispatch before the final `return 1`:

```python
    if args.mode == "versions":
        return print_versions()
```

- [ ] **Step 5: Run version tests and command**

Run:

```powershell
uv run --extra eval pytest tests/core/test_versioning.py tests/test_cli_versions.py -v
uv run python -m eee_agent.cli versions
```

Expected: tests pass and stdout is valid JSON containing the locked versions.

- [ ] **Step 6: Commit version diagnostics**

```powershell
git add eee_agent/core eee_agent/cli.py tests/core/test_versioning.py tests/test_cli_versions.py
git commit -m "feat: report runtime dependency versions"
```

### Task 12: Configuration Documentation And Foundation Verification

**Files:**
- Modify: `.env.example`
- Create: `tests/test_env_example.py`

- [ ] **Step 1: Write the failing environment-example test**

Create `tests/test_env_example.py`:

```python
from pathlib import Path


def test_env_example_documents_strict_deepseek_transport() -> None:
    text = Path(".env.example").read_text(encoding="utf-8")
    assert "https://api.deepseek.com/anthropic" in text
    assert "EEE_LLM_THINKING=enabled" in text
    assert "EEE_LLM_EFFORT=max" in text
    assert "EEE_LLM_MAX_TOKENS=8192" in text
    assert "deepseek-chat" in text
    assert "do not use" in text.lower()
```

- [ ] **Step 2: Run the test to verify documentation is stale**

Run:

```powershell
uv run --extra eval pytest tests/test_env_example.py -v
```

Expected: `FAIL` because the Anthropic endpoint and thinking controls are missing.

- [ ] **Step 3: Replace the LLM section in `.env.example`**

Use this exact block:

```dotenv
# --- LLM provider (default: strict DeepSeek V4 Pro) ---
# EEE_LLM_PROVIDER in {deepseek, anthropic, openai}
EEE_LLM_PROVIDER=deepseek
EEE_LLM_MODEL=deepseek-v4-pro

# DeepSeek uses the official Anthropic-compatible endpoint:
# https://api.deepseek.com/anthropic
DEEPSEEK_API_KEY=sk-your-deepseek-key
EEE_LLM_THINKING=enabled
EEE_LLM_EFFORT=max
EEE_LLM_MAX_TOKENS=8192

# Exact DeepSeek names only: deepseek-v4-pro or deepseek-v4-flash.
# deepseek-chat and deepseek-reasoner are deprecated aliases; do not use them.

# Optional standard providers, selected only by EEE_LLM_PROVIDER.
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
```

- [ ] **Step 4: Run the complete offline Foundation suite**

Run:

```powershell
uv lock --check
uv sync --frozen --extra eval
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent houdini_side eval
$env:DEEPSEEK_API_KEY = "final-verification-placeholder-not-used"
uv run python -c "from eee_agent.app import build_agent; g=build_agent(); assert 'task' not in g.nodes['tools'].bound._tools_by_name; print(type(g).__name__)"
Remove-Item Env:\DEEPSEEK_API_KEY
uv run python -m eee_agent.cli versions
bash scripts/env_probe.sh
```

Expected:

- `uv lock --check` exits 0.
- All pytest tests pass with no network or Houdini requirement.
- `compileall` exits 0.
- Agent construction prints `CompiledStateGraph`; `task` is absent.
- `versions` emits valid JSON.
- The environment probe finds `/mnt/d/houdini` on this machine and exits 0; RPC may correctly report as not listening.

- [ ] **Step 5: Review the diff for scope and secret leakage**

Run:

```powershell
git diff --check
git status --short
git diff --stat main...HEAD
git grep -n "unit-test-key\|placeholder-not-used" -- ':!tests/**' ':!scripts/env_probe.sh'
```

Expected: no whitespace errors; only Foundation files are changed; the final grep has no product-code or config matches containing test placeholder values.

- [ ] **Step 6: Commit configuration documentation**

```powershell
git add .env.example tests/test_env_example.py
git commit -m "docs: document strict provider settings"
```

- [ ] **Step 7: Record final branch state**

Run:

```powershell
git status --short
git log --oneline --decorate -12
```

Expected: worktree is clean and the Foundation branch contains one focused commit per task group.

## Foundation Exit Criteria

Foundation is complete only when all conditions below are true:

1. `uv.lock` is committed and `uv lock --check` succeeds.
2. All direct dependency versions match the tested versions, including `rpyc==4.1.0`.
3. `scripts/env_probe.sh` finds the WSL `/mnt/d/houdini` layout and remains read-only/non-fatal.
4. Core IDs, errors, events, artifact refs, and version reports have deterministic unit tests.
5. Model construction flows through `ProviderRegistry`; `eee_agent.model` imports no concrete provider class.
6. DeepSeek V4 uses `ChatAnthropic` with `https://api.deepseek.com/anthropic`, exact model names, thinking enabled, effort max, and image input disabled.
7. The no-network payload contract proves `thinking` and `tool_use` blocks survive before `tool_result` replay.
8. Provider streaming chunks normalize into provider-neutral reasoning, text, tool, and usage events.
9. The compiled Deep Agent has no implicit `task` tool or inherited `general-purpose` subagent.
10. Existing Anthropic and OpenAI selections still construct their native LangChain classes.
11. `python -m eee_agent.cli versions` reports all reproducibility-critical versions.
12. The full offline pytest suite, compileall, Agent graph smoke test, and environment probe pass.

## Deferred To Later Milestones

- Native Deep Agents skill loading is deferred until Runtime can seed a thread-scoped backend and freeze loaded skill hashes per Run. Foundation keeps the current embedded prompt path without expanding it.
- Provider live verification, verification caching, Settings UI, TOML profiles, Credential Manager, and session-level role bindings are Runtime/UI work.
- Full reasoning retention/compaction and event replay require the Runtime event store and checkpointer.
- Permission enforcement, typed Houdini ChangeSets, and removal of low-level scene write tools belong to Secure Houdini Bridge.
- Vision capability detection and dedicated vision routing belong to Capture, Vision, and Eval.
