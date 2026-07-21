# Runtime Panel Three-Pane Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the Houdini Runtime panel view layer as a three-pane, conversation-centric agent panel using the Houdini 22 Pluto visual language, with a fully automatic backend startup chain.

**Architecture:** The 2134-line `houdini_side/runtime_panel.py` becomes a `houdini_side/runtime_panel/` package. All testable logic lives in Qt-free modules (`theme.py`, `view_models.py`, `backend_launcher.py`); Qt widgets are thin shells verified by source-boundary tests (the established pattern — PySide6 is not and must not become a project dependency). The pure state layer `eee_agent/panel/*` and the Runtime protocol are untouched. Approved spec: `docs/superpowers/specs/2026-07-21-runtime-panel-three-pane-design.md`.

**Tech Stack:** Python 3.11, PySide6 (Houdini 21 ships Qt 6.5.3 — no Qt 6.6+ APIs), pytest, existing `eee_agent/panel` client/state modules.

**Workspace:** `E:\eee-agent\.worktrees\runtime`, branch `feature/runtime`. All commands below run from that root with Git Bash unless noted. Test runner: `uv run --frozen --extra eval pytest -q <path>`.

**Key facts the engineer must know:**

- `eee_agent/panel/client_state.py` provides `runtime_state_dir()`, `load_runtime_credentials(state_dir)` (raises `PanelClientError("Runtime is not running.")` when discovery is absent), `build_command`, `parse_runtime_message`.
- `eee_agent/panel/runtime_state.py` provides `parse_vision_event(message)`, `parse_artifact_event(message)`, `append_artifact_summary(...)`, `append_vision_summary(...)`, `parse_changeset_list(result)`, `approval_is_actionable(...)`, `RuntimePanelState`.
- The existing `RuntimeObserverClient` (Qt QObject, WebSocket client) and its signals — `connectionChanged(str,str)`, `sessionChanged(str,str,int)`, `sessionsChanged(object,str)`, `runtimeSnapshotChanged(object)`, `changesetsChanged(object)`, `commandSucceeded(str,object)`, `commandFailed(str,str,str,bool,bool)`, `eventObserved(str,int)`, `artifactObserved(object)`, `visionObserved(object)` — move unchanged into the package.
- The backend is spawned as `<venv python> -m eee_agent.runtime serve` (default host/port are correct; do not pass flags). It publishes `runtime.json` + `runtime.token` into the state dir.
- H22 Pluto design tokens were extracted from `/d/Houdini22/houdini/config/Themes/PlutoThemeDefaults.json`; the exact values are in Task 2 and nowhere else.

---

### Task 1: Mechanical migration — old single file becomes a package (behavior unchanged)

The name `houdini_side.runtime_panel` must resolve to a package. Python prefers packages over same-named modules, so the old file must move in the same commit. Behavior must not change; this is a pure move.

**Files:**
- Create: `houdini_side/runtime_panel/__init__.py`
- Create: `houdini_side/runtime_panel/legacy.py` (full content of the old `houdini_side/runtime_panel.py`, unchanged)
- Create: `houdini_side/runtime_panel/client.py`
- Delete: `houdini_side/runtime_panel.py`
- Modify: `tests/panel/test_panel_package.py:25-46` (source path changes)

- [ ] **Step 1: Move the old file and create the package**

```bash
cd /e/eee-agent/.worktrees/runtime
mkdir -p houdini_side/runtime_panel
git mv houdini_side/runtime_panel.py houdini_side/runtime_panel/legacy.py
```

- [ ] **Step 2: Split the client classes out of legacy.py into client.py**

Move (do not edit) these definitions from `legacy.py` into `houdini_side/runtime_panel/client.py`: `_configure_ime`, `_load_preferred_session_id`, `_save_preferred_session_id`, `SessionTitleDialog`, `RunRequestEdit`, `SelectionQueryWorker`, `RuntimeObserverClient`. `client.py` keeps the same imports as the old file header plus `from eee_agent.panel.client_state import ...` and `from houdini_side.secure_bridge_host import ...` exactly as they appear in `legacy.py` today. In `legacy.py`, replace the moved definitions with:

```python
from houdini_side.runtime_panel.client import (  # noqa: E402
    RunRequestEdit,
    RuntimeObserverClient,
    SelectionQueryWorker,
    SessionTitleDialog,
    _configure_ime,
    _load_preferred_session_id,
    _save_preferred_session_id,
)
```

- [ ] **Step 3: Create `__init__.py` re-exporting the public contract**

```python
"""Three-pane Runtime panel package. Public pypanel contract lives here."""

from __future__ import annotations

from houdini_side.runtime_panel.legacy import (  # noqa: F401
    RuntimePanel,
    create_panel,
    open_panel,
)
```

- [ ] **Step 4: Update the source-boundary test path**

In `tests/panel/test_panel_package.py`, change the two occurrences of `ROOT / "houdini_side" / "runtime_panel.py"` to `ROOT / "houdini_side" / "runtime_panel" / "legacy.py"`. Every other assertion in that file is unchanged (the source text moved verbatim, so they still pass).

- [ ] **Step 5: Run the panel tests**

Run: `uv run --frozen --extra eval pytest -q tests/panel -x`
Expected: all PASS (same count as before the move, currently 4 files).

- [ ] **Step 6: Commit**

```bash
git add houdini_side/runtime_panel houdini_side/runtime_panel.py tests/panel/test_panel_package.py
git commit -m "refactor: migrate runtime panel module into a package (no behavior change)"
```

---

### Task 2: `theme.py` — Pluto design tokens, QSS builder, font/icon helpers (Qt-free core)

**Files:**
- Create: `houdini_side/runtime_panel/theme.py`
- Test: `tests/panel/test_runtime_panel_theme.py`

- [ ] **Step 1: Write the failing test**

```python
"""Token/QSS contract for the Pluto theme module (Qt-free)."""

from __future__ import annotations

import re

from houdini_side.runtime_panel import theme


def test_pluto_core_tokens_match_h22_defaults() -> None:
    assert theme.BG == "#2d2d2d"
    assert theme.SURFACE_LOWEST == "#242424"
    assert theme.SURFACE_HIGHEST == "#383838"
    assert theme.FG == "#dddddd"
    assert theme.FG_DIM == "#7f7f7f"
    assert theme.FIELD == "#434343"
    assert theme.BUTTON == "#474e62"
    assert theme.BUTTON_HOVER == "#777f95"
    assert theme.PRESSED == "#313f71"
    assert theme.PRIMARY == "#7082b9"
    assert theme.CHECKED_SURFACE == "#47578b"
    assert theme.HIGHLIGHT == "#fdba00"
    assert theme.HIGHLIGHT_FG == "#271900"
    assert theme.DIVIDER == "#202020"


def test_semantic_state_tokens() -> None:
    assert theme.STATUS_OK == "#73d114"
    assert theme.STATUS_WARN == "#f87431"
    assert theme.STATUS_ERROR == "#cc0000"


def test_qss_contains_tokens_and_no_unresolved_placeholders() -> None:
    qss = theme.build_qss()
    assert "#2d2d2d" in qss
    assert "#fdba00" in qss
    assert "QTabBar::tab" in qss
    assert "QPushButton" in qss
    assert not re.search(r"@[A-Za-z]+@", qss), "unresolved @Token@ placeholder"


def test_all_tokens_are_hex_colors() -> None:
    for name in dir(theme):
        if name.isupper() and name not in {"UI_FONT", "MONO_FONT"}:
            value = getattr(theme, name)
            if isinstance(value, str):
                assert re.fullmatch(r"#[0-9a-f]{6}", value), name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_theme.py -x`
Expected: FAIL with `ModuleNotFoundError` or `AttributeError: module ... has no attribute 'BG'`.

- [ ] **Step 3: Implement theme.py**

```python
"""Houdini 22 Pluto dark-theme design tokens and QSS builder.

Qt-free: tokens and ``build_qss`` are importable in the plain test venv.
Qt helpers (font registration, icon lookup) import PySide6 lazily inside
functions so this module never breaks the frozen test environment.

Token source: $HFS/houdini/config/Themes/PlutoThemeDefaults.json (default
Pluto dark palette) and $HFS/houdini/config/Styles/base.qss (metrics).
No hex color literal may appear in any other runtime_panel module.
"""

from __future__ import annotations

# --- surfaces -------------------------------------------------------------
BG = "#2d2d2d"
SURFACE_LOWEST = "#242424"
SURFACE_1 = "#282828"
SURFACE_2 = "#313131"
SURFACE_3 = "#333333"
SURFACE_4 = "#353535"
SURFACE_HIGHEST = "#383838"
TOOLTIP_SURFACE = "#171717"
DIVIDER = "#202020"
PANE_DIVIDER = "#222222"

# --- text -----------------------------------------------------------------
FG = "#dddddd"
FG_PROMINENT = "#ffffff"
FG_DIM = "#7f7f7f"
FG_DIMMER = "#5a5a5a"
FIELD_FG = "#f9f9f9"

# --- fields / views -------------------------------------------------------
FIELD = "#434343"
FIELD_ALT = "#3e3e3e"
VIEW_SURFACE = "#2f2f2f"
VIEW_SURFACE_ALT = "#353535"

# --- buttons / selection --------------------------------------------------
BUTTON = "#474e62"
BUTTON_HOVER = "#777f95"
PRESSED = "#313f71"
PRESSED_FG = "#f7f9ff"
PRIMARY = "#7082b9"
SECONDARY = "#7c849a"
CHECKED_SURFACE = "#47578b"
CHECKED_FG = "#e3ebff"

# --- approval gate amber (reserved for pending authorization) -------------
HIGHLIGHT = "#fdba00"
HIGHLIGHT_FG = "#271900"
HIGHLIGHT_SURFACE = "#755400"

# --- semantic status (H22 UIDark performance/status colors) ---------------
STATUS_OK = "#73d114"
STATUS_WARN = "#f87431"
STATUS_ERROR = "#cc0000"

# --- fonts ----------------------------------------------------------------
UI_FONT = "SideFX Source Sans Pro"     # fallback: Segoe UI
MONO_FONT = "SideFX Source Code Pro"   # fallback: Consolas
UI_FONT_FALLBACK = "Segoe UI"
MONO_FONT_FALLBACK = "Consolas"

_QSS = """
QWidget {{ background: {bg}; color: {fg}; font-size: 9pt; }}
QLabel#DimLabel {{ color: {fg_dim}; }}
QLabel#ProminentLabel {{ color: {fg_prominent}; font-weight: 600; }}
QFrame#Card {{ background: {surface2}; border: 1px solid {divider};
    border-radius: 5px; }}
QFrame#GateCard {{ background: {surface2}; border: 1px solid {highlight};
    border-radius: 5px; }}
QPushButton {{ background: {button}; color: {fg}; border: none;
    border-radius: 4px; padding: 2px 15px; min-height: 17px; }}
QPushButton:hover {{ background: {button_hover}; }}
QPushButton:pressed {{ background: {pressed}; color: {pressed_fg}; }}
QPushButton:disabled {{ background: {surface2}; color: {fg_dimmer}; }}
QPushButton#GateApprove {{ background: {highlight}; color: {highlight_fg}; }}
QPushButton#GateApprove:hover {{ background: {highlight_surface};
    color: {fg_prominent}; }}
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{
    background: {field}; color: {field_fg}; border: 1px solid {divider};
    border-radius: 2px; padding: 1px 4px; min-height: 17px;
    selection-background-color: {checked_surface};
    selection-color: {checked_fg}; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {primary}; }}
QListWidget, QTreeWidget, QTableWidget {{
    background: {view_surface}; color: {fg};
    border: 1px solid {divider};
    alternate-background-color: {view_surface_alt}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background: {checked_surface}; color: {checked_fg}; }}
QTabWidget::pane {{ border: 1px solid {divider}; }}
QTabBar::tab {{ background: {surface1}; color: {fg_dim};
    padding: 2px 7px; min-height: 20px; border: none; }}
QTabBar::tab:selected {{ background: {bg}; color: {fg_prominent}; }}
QTabBar::tab:hover {{ background: {surface2}; }}
QSplitter::handle {{ background: {pane_divider}; }}
QScrollBar:vertical {{ background: {surface_lowest}; width: 15px; }}
QScrollBar::handle:vertical {{ background: {button}; min-height: 30px;
    border-radius: 4px; }}
QScrollBar::handle:vertical:hover {{ background: {button_hover}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QToolTip {{ background: {tooltip_surface}; color: {fg};
    border: 1px solid {divider}; }}
"""


def build_qss() -> str:
    """Render the panel stylesheet from the tokens above."""
    return _QSS.format(
        bg=BG, fg=FG, fg_dim=FG_DIM, fg_dimmer=FG_DIMMER,
        fg_prominent=FG_PROMINENT, field=FIELD, field_fg=FIELD_FG,
        surface_lowest=SURFACE_LOWEST, surface1=SURFACE_1,
        surface2=SURFACE_2, divider=DIVIDER, pane_divider=PANE_DIVIDER,
        button=BUTTON, button_hover=BUTTON_HOVER, pressed=PRESSED,
        pressed_fg=PRESSED_FG, primary=PRIMARY,
        checked_surface=CHECKED_SURFACE, checked_fg=CHECKED_FG,
        highlight=HIGHLIGHT, highlight_fg=HIGHLIGHT_FG,
        highlight_surface=HIGHLIGHT_SURFACE, view_surface=VIEW_SURFACE,
        view_surface_alt=VIEW_SURFACE_ALT, tooltip_surface=TOOLTIP_SURFACE,
    )


def register_fonts() -> None:
    """Register SideFX fonts from $HFS/houdini/fonts when inside Houdini.

    No-op outside Houdini; PySide6 is imported lazily so the test venv
    (no Qt) can import this module.
    """
    try:
        import hou  # type: ignore
        from PySide6 import QtGui
    except ImportError:
        return
    import os

    fonts_dir = os.path.join(os.environ.get("HFS", ""), "houdini", "fonts")
    if not os.path.isdir(fonts_dir):
        return
    for name in os.listdir(fonts_dir):
        if name.lower().endswith((".ttf", ".otf")):
            QtGui.QFontDatabase.addApplicationFont(os.path.join(fonts_dir, name))


def ui_font_family() -> str:
    return f"{UI_FONT}, {UI_FONT_FALLBACK}"


def mono_font_family() -> str:
    return f"{MONO_FONT}, {MONO_FONT_FALLBACK}"


def icon(name: str):
    """Resolve a built-in Houdini icon, e.g. icon("BUTTONS_add.svg")."""
    import hou  # type: ignore

    return hou.qt.Icon(name, None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_theme.py -x`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add houdini_side/runtime_panel/theme.py tests/panel/test_runtime_panel_theme.py
git commit -m "feat: add Pluto design-token theme module for the runtime panel"
```

---

### Task 3: `view_models.py` — Qt-free message items and context status

All conversation/activity rendering decisions are made here so they are unit-testable without Qt.

**Files:**
- Create: `houdini_side/runtime_panel/view_models.py`
- Test: `tests/panel/test_runtime_panel_view_models.py`

- [ ] **Step 1: Write the failing test**

```python
"""Message-item and status aggregation contracts (Qt-free)."""

from __future__ import annotations

from houdini_side.runtime_panel import view_models as vm


def test_user_message_is_bounded() -> None:
    item = vm.user_message("x" * 5000)
    assert item.kind == "user"
    assert len(item.body) == vm.MAX_BODY_CHARS
    assert item.tone == "normal"


def test_proposal_card_summarizes_operations() -> None:
    item = vm.proposal_card(
        {"operation_count": 14, "permission_mode": "ProjectChange",
         "changeset_digest": "ab" * 32}
    )
    assert item.kind == "proposal"
    assert "14" in item.title
    assert "ProjectChange" in item.body
    assert item.body.count("ab" * 8) == 1  # digest shown as 16-char prefix
    assert item.tone == "gate"


def test_vision_card_maps_status_to_tone() -> None:
    ok = vm.vision_card({"vision_status": "completed", "decision": "accepted",
                         "report_summary": "matches brief"})
    assert ok.tone == "ok"
    failed = vm.vision_card({"vision_status": "unavailable",
                             "decision": "unavailable",
                             "report_summary": None})
    assert failed.tone == "warn"
    assert failed.body  # bounded fallback text, never empty


def test_artifact_card_lists_state() -> None:
    item = vm.artifact_card(
        {"artifact_id": "art_" + "0" * 32, "relative_path": "shots/apply.png",
         "state": "available", "size_bytes": 2048}
    )
    assert item.kind == "artifact"
    assert "shots/apply.png" in item.title
    assert "available" in item.body


def test_append_bounded_enforces_cap() -> None:
    items: list[vm.MessageItem] = []
    for _ in range(vm.MAX_MESSAGES + 10):
        vm.append_bounded(items, vm.notice_card("n"))
    assert len(items) == vm.MAX_MESSAGES


def test_context_status_aggregates() -> None:
    status = vm.context_status(
        hip="untitled.hip", session_title="Table session",
        workspace_id=None, connection="online", bridge="ready",
        run_state="Planning",
    )
    assert status.runtime == "online"
    assert status.workspace == "no workspace"
    assert status.run_state == "Planning"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_view_models.py -x`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement view_models.py**

```python
"""Qt-free view models for the three-pane Runtime panel.

Every rendering decision (bounding, tone mapping, digest shortening) lives
here so it is unit-testable without PySide6. Inputs are plain mappings as
emitted by eee_agent.panel.runtime_state parsers; this module is defensive
(.get with defaults) and never raises on partial data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

MAX_MESSAGES = 200
MAX_BODY_CHARS = 2000
MAX_TITLE_CHARS = 120

TONES = frozenset({"normal", "ok", "warn", "error", "gate"})


@dataclass(frozen=True, slots=True)
class MessageItem:
    kind: str        # user|proposal|approval|validation|vision|artifact|notice|error
    title: str
    body: str
    tone: str        # one of TONES
    mono: bool = False


@dataclass(frozen=True, slots=True)
class ContextStatus:
    hip: str
    session: str
    workspace: str
    runtime: str
    bridge: str
    run_state: str


def _bounded(value: object, limit: int) -> str:
    text = value if type(value) is str else ""
    return text[:limit]


def _digest_prefix(value: object) -> str:
    text = value if type(value) is str else ""
    return text[:16]


def user_message(text: str) -> MessageItem:
    return MessageItem(
        kind="user", title="You",
        body=_bounded(text, MAX_BODY_CHARS), tone="normal",
    )


def proposal_card(payload: Mapping[str, object]) -> MessageItem:
    count = payload.get("operation_count")
    count_text = str(count) if type(count) is int else "?"
    mode = _bounded(payload.get("permission_mode"), 40) or "unknown mode"
    digest = _digest_prefix(payload.get("changeset_digest"))
    body = f"Permission: {mode}"
    if digest:
        body += f"\nDigest: {digest}"
    return MessageItem(
        kind="proposal",
        title=_bounded(f"Proposal · {count_text} operations", MAX_TITLE_CHARS),
        body=body, tone="gate", mono=True,
    )


def approval_result_card(approved: bool, *, expired: bool = False) -> MessageItem:
    if expired:
        return MessageItem("approval", "Approval expired",
                           "The gate timed out without a decision.", "warn")
    if approved:
        return MessageItem("approval", "Approved",
                           "ChangeSet approved and queued for apply.", "ok")
    return MessageItem("approval", "Rejected",
                       "ChangeSet rejected by the user.", "error")


def validation_card(report: Mapping[str, object]) -> MessageItem:
    accepted = report.get("accepted") is True
    summary = _bounded(report.get("summary") or report.get("report_summary"),
                       MAX_BODY_CHARS)
    return MessageItem(
        kind="validation",
        title="Validation passed" if accepted else "Validation failed",
        body=summary or "No summary reported.",
        tone="ok" if accepted else "error",
    )


def vision_card(summary: Mapping[str, object]) -> MessageItem:
    status = _bounded(summary.get("vision_status"), 40) or "unknown"
    decision = _bounded(summary.get("decision"), 40) or "unknown"
    text = _bounded(summary.get("report_summary"), MAX_BODY_CHARS)
    tone = "ok" if status == "completed" and decision == "accepted" else "warn"
    body = f"Status: {status}\nDecision: {decision}"
    if text:
        body += f"\n{text}"
    return MessageItem(kind="vision", title="Vision evaluation",
                       body=body, tone=tone)


def artifact_card(summary: Mapping[str, object]) -> MessageItem:
    path = _bounded(summary.get("relative_path"), MAX_TITLE_CHARS) or "artifact"
    state = _bounded(summary.get("state"), 40) or "unknown"
    size = summary.get("size_bytes")
    size_text = f"{size} bytes" if type(size) is int else "size unknown"
    tone = "ok" if state == "available" else (
        "error" if state in {"failed", "missing"} else "normal")
    return MessageItem(
        kind="artifact", title=_bounded(path, MAX_TITLE_CHARS),
        body=f"State: {state}\n{size_text}", tone=tone, mono=True,
    )


def notice_card(text: str, *, tone: str = "warn") -> MessageItem:
    assert tone in TONES
    return MessageItem(kind="notice", title="Notice",
                       body=_bounded(text, MAX_BODY_CHARS), tone=tone)


def append_bounded(items: list[MessageItem], item: MessageItem) -> None:
    items.append(item)
    del items[: max(0, len(items) - MAX_MESSAGES)]


def context_status(
    *,
    hip: str | None,
    session_title: str | None,
    workspace_id: str | None,
    connection: str,
    bridge: str,
    run_state: str,
) -> ContextStatus:
    return ContextStatus(
        hip=hip or "no hip",
        session=session_title or "no session",
        workspace=(
            "ws " + workspace_id[3:11] if workspace_id else "no workspace"
        ),
        runtime=connection,
        bridge=bridge,
        run_state=run_state,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_view_models.py -x`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add houdini_side/runtime_panel/view_models.py tests/panel/test_runtime_panel_view_models.py
git commit -m "feat: add Qt-free conversation view models with bounded cards"
```

---

### Task 4: `backend_launcher.py` — automatic backend spawn (Qt-free)

**Files:**
- Create: `houdini_side/runtime_panel/backend_launcher.py`
- Test: `tests/panel/test_backend_launcher.py`

- [ ] **Step 1: Write the failing test**

```python
"""Backend launcher contract: probe, spawn, wait, diagnose (Qt-free)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

from eee_agent.panel.client_state import (
    RUNTIME_DISCOVERY_FILENAME,
    RUNTIME_TOKEN_FILENAME,
)
from houdini_side.runtime_panel import backend_launcher as bl


def _write_valid_discovery(state: Path, token: str = "t0ken") -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / RUNTIME_TOKEN_FILENAME).write_text(token, encoding="utf-8")
    fingerprint = hashlib.sha256(token.encode()).hexdigest()[:12]
    (state / RUNTIME_DISCOVERY_FILENAME).write_text(json.dumps({
        "protocol": "eee.runtime/1", "host": "127.0.0.1", "port": 45678,
        "pid": 1234, "process_nonce": "nonce",
        "token_file": RUNTIME_TOKEN_FILENAME,
        "token_fingerprint": fingerprint,
        "started_at": "2026-07-21T00:00:00+00:00",
    }), encoding="utf-8")


_READY_STUB = textwrap.dedent(
    """
    import hashlib, json, sys
    from pathlib import Path
    state = Path(sys.argv[1]); state.mkdir(parents=True, exist_ok=True)
    (state / "runtime.token").write_text("t0ken", encoding="utf-8")
    fp = hashlib.sha256(b"t0ken").hexdigest()[:12]
    (state / "runtime.json").write_text(json.dumps({
        "protocol": "eee.runtime/1", "host": "127.0.0.1", "port": 45678,
        "pid": 1234, "process_nonce": "nonce", "token_file": "runtime.token",
        "token_fingerprint": fp, "started_at": "2026-07-21T00:00:00+00:00",
    }), encoding="utf-8")
    """
)

_HANG_STUB = "import time; time.sleep(60)"


def _spawn_stub(command, **kwargs):
    # Replace the requested interpreter with the test interpreter running a
    # stub script that mimics backend discovery publishing.
    stub = Path(kwargs.pop("stub_path"))
    return subprocess.Popen(
        [sys.executable, str(stub), *command[1:]], **kwargs
    )


def test_probe_reports_not_running(tmp_path: Path) -> None:
    assert bl.discovery_ready(tmp_path) is False


def test_probe_reports_ready(tmp_path: Path) -> None:
    _write_valid_discovery(tmp_path)
    assert bl.discovery_ready(tmp_path) is True


def test_interpreter_candidates_prefer_repo_venv(tmp_path: Path) -> None:
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    candidates = bl.interpreter_candidates(tmp_path)
    assert candidates[0] == venv_python


def test_ensure_runtime_spawns_and_waits(tmp_path: Path) -> None:
    state = tmp_path / "state"
    stub = tmp_path / "stub.py"
    stub.write_text(_READY_STUB, encoding="utf-8")
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")

    def spawn(command, **kwargs):
        return subprocess.Popen([sys.executable, str(stub), str(state)],
                                **{k: v for k, v in kwargs.items()
                                   if k in {"stdout", "stderr", "cwd", "env"}})

    result = bl.ensure_runtime(tmp_path, state, timeout_seconds=5.0,
                               spawn=spawn)
    assert result.status == "spawned"
    assert result.process is not None
    result.process.wait(timeout=5)
    assert bl.discovery_ready(state) is True


def test_ensure_runtime_times_out_with_log(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    stub = tmp_path / "hang.py"
    stub.write_text(_HANG_STUB, encoding="utf-8")
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")

    def spawn(command, **kwargs):
        return subprocess.Popen([sys.executable, str(stub)],
                                **{k: v for k, v in kwargs.items()
                                   if k in {"stdout", "stderr", "cwd", "env"}})

    result = bl.ensure_runtime(tmp_path, state, timeout_seconds=0.3,
                               spawn=spawn)
    assert result.status == "timeout"
    assert result.log_path is not None and result.log_path.exists()
    assert result.process is not None
    bl.terminate(result.process)


def test_ensure_runtime_missing_interpreter(tmp_path: Path) -> None:
    def failing_spawn(command, **kwargs):
        raise FileNotFoundError(command[0])

    result = bl.ensure_runtime(tmp_path, tmp_path / "state",
                               timeout_seconds=0.1, spawn=failing_spawn)
    assert result.status == "missing_interpreter"


def test_terminate_is_graceful_then_force() -> None:
    proc = subprocess.Popen([sys.executable, "-c", _HANG_STUB])
    bl.terminate(proc, timeout=1.0)
    assert proc.poll() is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_backend_launcher.py -x`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement backend_launcher.py**

```python
"""Automatic Runtime backend startup for the panel (Qt-free).

Discovery contract: the backend publishes runtime.json + runtime.token into
the state dir (see eee_agent.panel.client_state.load_runtime_credentials).
The launcher probes, spawns the repo venv interpreter, waits for discovery,
and classifies the outcome for the panel's offline card. Whoever spawns,
reaps: a panel-spawned process must be passed to ``terminate`` on panel
teardown; a pre-existing backend is never killed.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from eee_agent.panel.client_state import (
    PanelClientError,
    load_runtime_credentials,
)

MAX_LOG_BYTES = 65536
DEFAULT_TIMEOUT_SECONDS = 10.0

SpawnFn = Callable[..., subprocess.Popen]


@dataclass(slots=True)
class LaunchResult:
    status: str  # ready|spawned|timeout|failed|missing_interpreter
    detail: str
    process: subprocess.Popen | None = None
    log_path: Path | None = None


def discovery_ready(state_dir: Path) -> bool:
    try:
        load_runtime_credentials(state_dir)
    except PanelClientError:
        return False
    return True


def interpreter_candidates(repo_root: Path) -> list[Path]:
    return [
        repo_root / ".venv" / "Scripts" / "python.exe",  # Windows venv
        repo_root / ".venv" / "bin" / "python",          # POSIX fallback
    ]


def build_spawn_command(python_exe: Path) -> list[str]:
    return [str(python_exe), "-m", "eee_agent.runtime", "serve"]


def _open_bounded_log(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "runtime.launch.log"
    if log_path.exists() and log_path.stat().st_size > MAX_LOG_BYTES:
        log_path.write_bytes(b"")
    # Text mode so the backend's stderr is human-readable in the GUI gate.
    return log_path.open("ab"), log_path


def ensure_runtime(
    repo_root: Path,
    state_dir: Path,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    spawn: SpawnFn = subprocess.Popen,
) -> LaunchResult:
    """Ensure a Runtime backend is reachable; spawn one if needed."""
    if discovery_ready(state_dir):
        return LaunchResult(status="ready", detail="Runtime already running.")
    candidates = [p for p in interpreter_candidates(repo_root) if p.exists()]
    if not candidates:
        return LaunchResult(
            status="missing_interpreter",
            detail="No project virtualenv interpreter found under EEE_PATH.",
        )
    log_file, log_path = _open_bounded_log(state_dir)
    env = dict(os.environ)
    env.setdefault("EEE_RUNTIME_HOME", str(state_dir.parent))
    try:
        process = spawn(
            build_spawn_command(candidates[0]),
            stdout=log_file, stderr=log_file,
            cwd=str(repo_root), env=env,
        )
    except (OSError, FileNotFoundError) as exc:
        log_file.close()
        return LaunchResult(
            status="missing_interpreter",
            detail=f"Failed to start the Runtime interpreter: {exc}",
            log_path=log_path,
        )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if discovery_ready(state_dir):
            return LaunchResult(status="spawned",
                                detail="Runtime started by the panel.",
                                process=process, log_path=log_path)
        if process.poll() is not None:
            return LaunchResult(
                status="failed",
                detail=f"Runtime exited with code {process.returncode}.",
                process=process, log_path=log_path,
            )
        time.sleep(0.1)
    return LaunchResult(
        status="timeout",
        detail="Runtime did not publish discovery before the deadline.",
        process=process, log_path=log_path,
    )


def terminate(process: subprocess.Popen, *, timeout: float = 5.0) -> None:
    """Gracefully stop a panel-spawned backend, then force if needed."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_backend_launcher.py -x`
Expected: 7 passed. (If the `ensure_runtime` spawned test is slow, check the stub writes discovery before the wait loop's first poll — it polls every 0.1s.)

- [ ] **Step 5: Commit**

```bash
git add houdini_side/runtime_panel/backend_launcher.py tests/panel/test_backend_launcher.py
git commit -m "feat: add automatic runtime backend launcher with discovery wait"
```

---

### Task 5: `context_bar.py` — top status bar (Qt thin shell)

**Files:**
- Create: `houdini_side/runtime_panel/context_bar.py`
- Test: `tests/panel/test_runtime_panel_sources.py` (new shared source-boundary test module; later tasks append to it)

- [ ] **Step 1: Write the failing source-boundary test**

```python
"""Source contracts for the Qt shells of the three-pane panel.

PySide6 is not a project dependency, so Qt widgets are verified by source
contracts here and by the manual GUI checklist inside Houdini.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "houdini_side" / "runtime_panel"

FORBIDDEN_IMPORTS = {
    "sqlite3", "aiosqlite", "rpyc", "hrpyc",
    "eee_agent.app", "eee_agent.runtime.database",
    "eee_agent.runtime.checkpoints", "eee_agent.changesets.service",
    "eee_agent.houdini_bridge.changeset_provider",
}
FORBIDDEN_TEXT = ("changeset.apply", "hou.selectedNodes", "asyncio.run",
                  "eval(", "exec(")


def _source(name: str) -> str:
    return (PKG / name).read_text(encoding="utf-8")


def _imports(name: str) -> set[str]:
    tree = ast.parse(_source(name))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_context_bar_uses_view_model_and_theme_only() -> None:
    source = _source("context_bar.py")
    assert "from houdini_side.runtime_panel.view_models import ContextStatus" in source
    assert "from houdini_side.runtime_panel import theme" in source
    assert "sidebarToggled" in source
    assert "inspectorToggled" in source
    assert "def set_status(self, status: ContextStatus)" in source


def test_qt_shells_keep_import_boundary() -> None:
    for name in ("context_bar.py", "session_sidebar.py", "conversation.py",
                 "approval_drawer.py", "inspector.py", "main_window.py"):
        path = PKG / name
        if not path.exists():
            continue
        assert _imports(name).isdisjoint(FORBIDDEN_IMPORTS), name
        for text in FORBIDDEN_TEXT:
            assert text not in _source(name), f"{name}: {text}"


def test_no_hex_colors_outside_theme() -> None:
    import re

    for path in PKG.glob("*.py"):
        if path.name in {"theme.py", "legacy.py", "client.py"}:
            continue
        for match in re.finditer(r"#[0-9a-fA-F]{6}\b", path.read_text("utf-8")):
            raise AssertionError(f"hex color {match.group()} in {path.name}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_sources.py -x`
Expected: FAIL with `FileNotFoundError` on `context_bar.py`.

- [ ] **Step 3: Implement context_bar.py**

```python
"""Top context bar: HIP > Session > Workspace and Runtime|Bridge|Run state."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from houdini_side.runtime_panel import theme
from houdini_side.runtime_panel.view_models import ContextStatus

_STATUS_TONES = {
    "online": theme.STATUS_OK,
    "ready": theme.STATUS_OK,
    "connecting": theme.STATUS_WARN,
    "reconnecting": theme.STATUS_WARN,
    "unavailable": theme.STATUS_ERROR,
    "offline": theme.STATUS_ERROR,
    "error": theme.STATUS_ERROR,
}


class ContextBar(QtWidgets.QWidget):
    """Two-row status strip with sidebar/inspector drawer toggles."""

    sidebarToggled = QtCore.Signal(bool)
    inspectorToggled = QtCore.Signal(bool)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ContextBar")
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 4)
        root.setSpacing(2)

        location = QtWidgets.QHBoxLayout()
        self.hip_label = QtWidgets.QLabel("no hip")
        self.hip_label.setObjectName("ProminentLabel")
        self.session_label = QtWidgets.QLabel("no session")
        self.workspace_label = QtWidgets.QLabel("no workspace")
        self.workspace_label.setObjectName("DimLabel")
        location.addWidget(self.hip_label)
        location.addWidget(QtWidgets.QLabel(">"))
        location.addWidget(self.session_label)
        location.addWidget(QtWidgets.QLabel(">"))
        location.addWidget(self.workspace_label)
        location.addStretch(1)
        self.sidebar_button = QtWidgets.QToolButton()
        self.sidebar_button.setText("Sessions")
        self.sidebar_button.setCheckable(True)
        self.sidebar_button.setChecked(True)
        self.sidebar_button.toggled.connect(self.sidebarToggled)
        self.inspector_button = QtWidgets.QToolButton()
        self.inspector_button.setText("Inspector")
        self.inspector_button.setCheckable(True)
        self.inspector_button.setChecked(True)
        self.inspector_button.toggled.connect(self.inspectorToggled)
        location.addWidget(self.sidebar_button)
        location.addWidget(self.inspector_button)
        root.addLayout(location)

        states = QtWidgets.QHBoxLayout()
        self.runtime_label = QtWidgets.QLabel()
        self.bridge_label = QtWidgets.QLabel()
        self.run_label = QtWidgets.QLabel()
        for label in (self.runtime_label, self.bridge_label, self.run_label):
            states.addWidget(label)
        states.addStretch(1)
        root.addLayout(states)

    def set_status(self, status: ContextStatus) -> None:
        self.hip_label.setText(status.hip)
        self.session_label.setText(status.session)
        self.workspace_label.setText(status.workspace)
        self._set_state(self.runtime_label, "Runtime", status.runtime)
        self._set_state(self.bridge_label, "Bridge", status.bridge)
        self.run_label.setText(f"Run: {status.run_state}")

    @staticmethod
    def _set_state(label: QtWidgets.QLabel, name: str, value: str) -> None:
        color = _STATUS_TONES.get(value, theme.FG_DIM)
        label.setText(f'{name}: <span style="color:{color}">{value}</span>')
```

Note: `_STATUS_TONES` references theme token *variables*, not hex literals, so the no-hex-outside-theme contract holds.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_sources.py -x`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add houdini_side/runtime_panel/context_bar.py tests/panel/test_runtime_panel_sources.py
git commit -m "feat: add three-pane context bar widget"
```

---

### Task 6: `session_sidebar.py` — session list pane (Qt thin shell)

**Files:**
- Create: `houdini_side/runtime_panel/session_sidebar.py`
- Modify: `tests/panel/test_runtime_panel_sources.py` (append one test)

- [ ] **Step 1: Write the failing test (append to test_runtime_panel_sources.py)**

```python
def test_session_sidebar_contract() -> None:
    source = _source("session_sidebar.py")
    assert "sessionChosen = QtCore.Signal(str)" in source
    assert "newSessionRequested = QtCore.Signal()" in source
    assert "def set_sessions(self, sessions" in source
    assert "SessionTitleDialog" in source  # session naming reuses proven dialog
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_sources.py::test_session_sidebar_contract -x`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Implement session_sidebar.py**

```python
"""Session Sidebar pane: list, select, create Runtime sessions."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets


class SessionSidebar(QtWidgets.QWidget):
    """Left pane: sessions newest-first, active marked, new-session button."""

    sessionChosen = QtCore.Signal(str)
    newSessionRequested = QtCore.Signal()

    def __init__(self, client, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SessionSidebar")
        self._client = client  # RuntimeObserverClient, for SessionTitleDialog
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        header = QtWidgets.QLabel("Sessions")
        header.setObjectName("ProminentLabel")
        layout.addWidget(header)
        self.list = QtWidgets.QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.currentRowChanged.connect(self._row_changed)
        layout.addWidget(self.list, 1)
        self.new_button = QtWidgets.QPushButton("New session")
        self.new_button.setAutoDefault(False)
        self.new_button.clicked.connect(self.newSessionRequested)
        layout.addWidget(self.new_button)
        self._updating = False

    def set_sessions(self, sessions, selected_id: str) -> None:
        """Render session dicts from the client's sessionsChanged signal."""
        self._updating = True
        try:
            self.list.clear()
            selected_row = -1
            for row, item in enumerate(sessions):
                title = item.get("title", "session")
                marker = "● " if item.get("session_id") == selected_id else ""
                row_item = QtWidgets.QListWidgetItem(marker + title)
                row_item.setData(QtCore.Qt.ItemDataRole.UserRole,
                                 item.get("session_id"))
                self.list.addItem(row_item)
                if item.get("session_id") == selected_id:
                    selected_row = row
            if selected_row >= 0:
                self.list.setCurrentRow(selected_row)
        finally:
            self._updating = False

    def _row_changed(self, row: int) -> None:
        if self._updating or row < 0:
            return
        item = self.list.item(row)
        session_id = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if type(session_id) is str and session_id:
            self.sessionChosen.emit(session_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_sources.py -x`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add houdini_side/runtime_panel/session_sidebar.py tests/panel/test_runtime_panel_sources.py
git commit -m "feat: add session sidebar pane"
```

---

### Task 7: `conversation.py` — message flow, activity cards, composer (Qt thin shell)

Renders `view_models.MessageItem` objects; contains zero parsing logic. Composer keeps the proven IME-safe `RunRequestEdit` from `client.py`.

**Files:**
- Create: `houdini_side/runtime_panel/conversation.py`
- Modify: `tests/panel/test_runtime_panel_sources.py` (append one test)

- [ ] **Step 1: Write the failing test (append)**

```python
def test_conversation_contract() -> None:
    source = _source("conversation.py")
    assert "sendRequested = QtCore.Signal(str)" in source
    assert "stopRequested = QtCore.Signal()" in source
    assert "def append_item(self, item: MessageItem)" in source
    assert "def set_composer_state(self, state: str)" in source
    assert "RunRequestEdit" in source          # IME-safe composer input
    assert "MAX_MESSAGES" in source            # bounded flow enforced
    assert "_TONE_COLORS" in source            # tones come from theme tokens
    assert "returnPressed.connect" not in source  # send only from the button
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_sources.py::test_conversation_contract -x`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Implement conversation.py**

```python
"""Conversation pane: bounded card flow plus an IME-safe composer."""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from houdini_side.runtime_panel import theme
from houdini_side.runtime_panel.client import RunRequestEdit
from houdini_side.runtime_panel.view_models import MAX_MESSAGES, MessageItem

_TONE_COLORS = {
    "normal": theme.DIVIDER,
    "ok": theme.STATUS_OK,
    "warn": theme.STATUS_WARN,
    "error": theme.STATUS_ERROR,
    "gate": theme.HIGHLIGHT,
}


class _Card(QtWidgets.QFrame):
    def __init__(self, item: MessageItem, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("GateCard" if item.tone == "gate" else "Card")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        title = QtWidgets.QLabel(item.title)
        title.setObjectName("ProminentLabel")
        layout.addWidget(title)
        body = QtWidgets.QLabel(item.body)
        body.setWordWrap(True)
        body.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        if item.mono:
            body.setFont(QtGui.QFont(theme.mono_font_family()))
        layout.addWidget(body)
        # Left tone stripe via stylesheet token reference.
        color = _TONE_COLORS[item.tone]
        self.setStyleSheet(
            f"QFrame#{self.objectName()} {{ border-left: 3px solid {color}; }}")


class ConversationView(QtWidgets.QWidget):
    """Center pane: append-only bounded card flow plus composer."""

    sendRequested = QtCore.Signal(str)
    stopRequested = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ConversationView")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.flow_host = QtWidgets.QWidget()
        self.flow = QtWidgets.QVBoxLayout(self.flow_host)
        self.flow.setContentsMargins(2, 2, 2, 2)
        self.flow.addStretch(1)
        self.scroll.setWidget(self.flow_host)
        layout.addWidget(self.scroll, 1)

        composer = QtWidgets.QHBoxLayout()
        self.input = RunRequestEdit()
        self.input.setPlaceholderText("Describe what to build…")
        self.send_button = QtWidgets.QPushButton("Send")
        self.send_button.setAutoDefault(False)
        self.send_button.clicked.connect(self._send)
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.setAutoDefault(False)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stopRequested)
        composer.addWidget(self.input, 1)
        composer.addWidget(self.send_button)
        composer.addWidget(self.stop_button)
        layout.addLayout(composer)
        self._cards: list[_Card] = []

    def append_item(self, item: MessageItem) -> None:
        card = _Card(item)
        self.flow.insertWidget(self.flow.count() - 1, card)
        self._cards.append(card)
        while len(self._cards) > MAX_MESSAGES:
            old = self._cards.pop(0)
            self.flow.removeWidget(old)
            old.deleteLater()
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def set_composer_state(self, state: str) -> None:
        """state: idle | running | stopping (mirrors Run state machine)."""
        self.send_button.setEnabled(state == "idle")
        self.input.setEnabled(state == "idle")
        self.stop_button.setEnabled(state == "running")
        self.stop_button.setText("Stopping…" if state == "stopping" else "Stop")

    def _send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self.sendRequested.emit(text)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_sources.py -x`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add houdini_side/runtime_panel/conversation.py tests/panel/test_runtime_panel_sources.py
git commit -m "feat: add conversation flow pane with bounded cards and composer"
```

---

### Task 8: `approval_drawer.py` — interrupting approval gate (Qt thin shell)

**Files:**
- Create: `houdini_side/runtime_panel/approval_drawer.py`
- Modify: `tests/panel/test_runtime_panel_sources.py` (append one test)

- [ ] **Step 1: Write the failing test (append)**

```python
def test_approval_drawer_contract() -> None:
    source = _source("approval_drawer.py")
    assert "approved = QtCore.Signal()" in source
    assert "rejected = QtCore.Signal()" in source
    assert "def show_changeset(self, summary)" in source
    assert "def hide_drawer(self)" in source
    assert "HIGHLIGHT" in source                    # amber gate strip
    assert "Approve and build" in source
    assert "Reject" in source
    assert "approval_is_actionable" in source       # digest binding enforced
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_sources.py::test_approval_drawer_contract -x`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Implement approval_drawer.py**

```python
"""Approval gate drawer: interrupts the conversation for a pending ChangeSet."""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from eee_agent.panel.runtime_state import approval_is_actionable
from houdini_side.runtime_panel import theme

_MAX_PATH_ROWS = 8


class ApprovalDrawer(QtWidgets.QFrame):
    """Right-anchored amber gate; Approve stays bound to the exact digest."""

    approved = QtCore.Signal()
    rejected = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ApprovalDrawer")
        self.setStyleSheet(
            f"QFrame#ApprovalDrawer {{ background: {theme.SURFACE_2};"
            f" border: 1px solid {theme.HIGHLIGHT}; border-radius: 5px; }}")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)

        self.headline = QtWidgets.QLabel("GATE / AWAITING APPROVAL")
        self.headline.setObjectName("ProminentLabel")
        self.headline.setStyleSheet(f"color: {theme.HIGHLIGHT};")
        layout.addWidget(self.headline)

        self.summary = QtWidgets.QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.paths = QtWidgets.QLabel("")
        self.paths.setFont(QtGui.QFont(theme.mono_font_family()))
        self.paths.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self.paths)

        buttons = QtWidgets.QHBoxLayout()
        self.reject_button = QtWidgets.QPushButton("Reject")
        self.reject_button.setAutoDefault(False)
        self.reject_button.clicked.connect(self.rejected)
        self.approve_button = QtWidgets.QPushButton("Approve and build")
        self.approve_button.setObjectName("GateApprove")
        self.approve_button.setAutoDefault(False)
        self.approve_button.clicked.connect(self.approved)
        buttons.addWidget(self.reject_button)
        buttons.addStretch(1)
        buttons.addWidget(self.approve_button)
        layout.addLayout(buttons)
        self._actionable = False
        self.hide()

    def show_changeset(self, summary) -> None:
        """Render one actionable ChangeSet summary from the client."""
        self._actionable = approval_is_actionable(summary)
        title = summary.get("title") or "ChangeSet"
        count = summary.get("operation_count")
        mode = summary.get("permission_mode") or "unknown"
        self.summary.setText(f"{title} · {count} operations · {mode}")
        paths = summary.get("paths") or []
        rows = [str(p) for p in paths[:_MAX_PATH_ROWS]]
        extra = len(paths) - len(rows)
        if extra > 0:
            rows.append(f"+ {extra} more paths")
        self.paths.setText("\n".join(rows))
        self.approve_button.setEnabled(self._actionable)
        self.show()
        self.raise_()

    def hide_drawer(self) -> None:
        self._actionable = False
        self.hide()

    @property
    def actionable(self) -> bool:
        return self._actionable
```

If `approval_is_actionable` has a different signature than `(summary) -> bool`, check `eee_agent/panel/runtime_state.py:882` first and adapt this one call site — it is the same call the legacy panel makes in `_render_approval`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_sources.py -x`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add houdini_side/runtime_panel/approval_drawer.py tests/panel/test_runtime_panel_sources.py
git commit -m "feat: add interrupting approval gate drawer"
```

---

### Task 9: `inspector.py` — Run / Workspace / Validation / Artifacts tabs (Qt thin shell)

**Files:**
- Create: `houdini_side/runtime_panel/inspector.py`
- Modify: `tests/panel/test_runtime_panel_sources.py` (append one test)

- [ ] **Step 1: Write the failing test (append)**

```python
def test_inspector_contract() -> None:
    source = _source("inspector.py")
    for tab in ('"RUN"', '"WORKSPACE"', '"VALIDATION"', '"ARTIFACTS"'):
        assert tab in source
    assert "def set_run_snapshot(self, snapshot)" in source
    assert "def set_workspace_facts(self, facts)" in source
    assert "def set_validation_report(self, report)" in source
    assert "def render_artifacts(self, summaries)" in source
    assert "def render_visions(self, summaries)" in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_sources.py::test_inspector_contract -x`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Implement inspector.py**

```python
"""Inspector pane: Run / Workspace / Validation / Artifacts tabs."""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from houdini_side.runtime_panel import theme

_MAX_ROWS = 200


def _text_view(parent: QtWidgets.QWidget) -> QtWidgets.QPlainTextEdit:
    view = QtWidgets.QPlainTextEdit()
    view.setReadOnly(True)
    view.setFont(QtGui.QFont(theme.mono_font_family()))
    return view


class InspectorPane(QtWidgets.QWidget):
    """Right pane: structured read-only views of runtime state."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("InspectorPane")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs)

        self.run_view = _text_view(self)
        self.tabs.addTab(self.run_view, "RUN")
        self.workspace_view = _text_view(self)
        self.tabs.addTab(self.workspace_view, "WORKSPACE")
        self.validation_view = _text_view(self)
        self.tabs.addTab(self.validation_view, "VALIDATION")
        self.artifacts_view = _text_view(self)
        self.tabs.addTab(self.artifacts_view, "ARTIFACTS")

    @staticmethod
    def _dump(view: QtWidgets.QPlainTextEdit, rows: list[str]) -> None:
        view.setPlainText("\n".join(rows[:_MAX_ROWS]) or "No data.")

    def set_run_snapshot(self, snapshot) -> None:
        if not snapshot:
            self._dump(self.run_view, [])
            return
        rows = [
            f"run_id: {snapshot.get('run_id', '-')}",
            f"state: {snapshot.get('state', '-')}",
            f"model: {snapshot.get('model', '-')}",
            f"started: {snapshot.get('started_at', '-')}",
            f"tokens: {snapshot.get('usage', '-')}",
        ]
        self._dump(self.run_view, rows)

    def set_workspace_facts(self, facts) -> None:
        if not facts:
            self._dump(self.workspace_view, [])
            return
        rows = [f"{key}: {value}" for key, value in sorted(facts.items())]
        self._dump(self.workspace_view, rows)

    def set_validation_report(self, report) -> None:
        if not report:
            self._dump(self.validation_view, [])
            return
        rows = [f"accepted: {report.get('accepted', '-')}"]
        for failure in report.get("failures") or []:
            rows.append(f"- {failure}")
        self._dump(self.validation_view, rows)

    def render_artifacts(self, summaries) -> None:
        rows = [
            f"{s.get('state', '-'):16} {s.get('relative_path', '-')}"
            for s in summaries
        ]
        self._dump(self.artifacts_view, rows)

    def render_visions(self, summaries) -> None:
        rows = [
            f"[vision] {s.get('vision_status', '-')} / {s.get('decision', '-')}: "
            f"{s.get('report_summary') or '-'}"
            for s in summaries
        ]
        self._dump(self.artifacts_view, rows)
```

Note: `render_visions` renders into the ARTIFACTS tab with a `[vision] ` prefix, matching the legacy behavior where the artifacts tab carries a VISION EVALUATIONS section (`_render_visions` in the legacy file). Do not add a fifth tab.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_sources.py -x`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add houdini_side/runtime_panel/inspector.py tests/panel/test_runtime_panel_sources.py
git commit -m "feat: add inspector pane with run/workspace/validation/artifacts tabs"
```

---

### Task 10: `main_window.py` — three-pane assembly, responsive drawers, backend auto-start, client wiring

This is the integration task. The new `RuntimePanel` keeps the legacy class name so `create_panel()` callers are unaffected. All logic delegates to the client (unchanged), view_models, backend_launcher, and the pane widgets built in Tasks 5–9.

**Files:**
- Create: `houdini_side/runtime_panel/main_window.py`
- Modify: `houdini_side/runtime_panel/__init__.py`
- Modify: `tests/panel/test_runtime_panel_sources.py` (append two tests)

- [ ] **Step 1: Write the failing tests (append)**

```python
def test_main_window_contract() -> None:
    source = _source("main_window.py")
    assert "class RuntimePanel(QtWidgets.QWidget)" in source
    assert "QSplitter" in source
    assert "_WIDE_MIN_WIDTH = 900" in source
    assert "_MEDIUM_MIN_WIDTH = 700" in source
    assert "def resizeEvent(self, event)" in source
    assert "ensure_runtime" in source           # backend auto-start
    assert "backend_launcher.terminate" in source  # who spawns, reaps
    assert "context_bar.set_status" in source
    assert "conversation.append_item" in source
    assert "approval_drawer.show_changeset" in source
    assert 'def create_panel()' not in source  # factory stays in __init__


def test_package_init_exposes_create_panel() -> None:
    source = _source("__init__.py")
    assert "from houdini_side.runtime_panel.main_window import RuntimePanel" in source
    assert "def create_panel()" in source
    assert "theme.register_fonts()" in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --frozen --extra eval pytest -q tests/panel/test_runtime_panel_sources.py -x`
Expected: FAIL — `main_window.py` missing, and `__init__.py` still re-exports from `legacy`.

- [ ] **Step 3: Implement main_window.py**

```python
"""Three-pane Runtime panel: assembly, responsive drawers, client wiring."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from eee_agent.panel.client_state import runtime_state_dir
from eee_agent.panel.runtime_state import (
    parse_artifact_event,
    parse_vision_event,
)
from houdini_side.runtime_panel import backend_launcher, theme, view_models
from houdini_side.runtime_panel.approval_drawer import ApprovalDrawer
from houdini_side.runtime_panel.client import RuntimeObserverClient
from houdini_side.runtime_panel.context_bar import ContextBar
from houdini_side.runtime_panel.conversation import ConversationView
from houdini_side.runtime_panel.inspector import InspectorPane
from houdini_side.runtime_panel.session_sidebar import SessionSidebar

_WIDE_MIN_WIDTH = 900
_MEDIUM_MIN_WIDTH = 700


class RuntimePanel(QtWidgets.QWidget):
    """Three-pane agent panel. Thin shell: all logic stays in the client
    and the Qt-free view models / launcher."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EEEAgentRuntimePanel")
        self.setStyleSheet(theme.build_qss())

        self._client = RuntimeObserverClient(self)
        self._spawned_process = None
        self._connection = "offline"
        self._bridge = "unavailable"
        self._run_state = "idle"
        self._session_title = ""
        self._workspace_id: str | None = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.context_bar = ContextBar(self)
        layout.addWidget(self.context_bar)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.session_sidebar = SessionSidebar(self._client, self)
        self.conversation = ConversationView(self)
        self.inspector = InspectorPane(self)
        self.splitter.addWidget(self.session_sidebar)
        self.splitter.addWidget(self.conversation)
        self.splitter.addWidget(self.inspector)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([220, 600, 300])
        layout.addWidget(self.splitter, 1)

        self.approval_drawer = ApprovalDrawer(self.conversation)
        self.approval_drawer.hide()

        self._wire()
        self._start_backend_and_connect()

    # -- wiring ---------------------------------------------------------

    def _wire(self) -> None:
        c = self._client
        c.connectionChanged.connect(self._on_connection)
        c.sessionsChanged.connect(self._on_sessions)
        c.sessionChanged.connect(self._on_session)
        c.runtimeSnapshotChanged.connect(self._on_snapshot)
        c.changesetsChanged.connect(self._on_changesets)
        c.artifactObserved.connect(self._on_artifact)
        c.visionObserved.connect(self._on_vision)
        c.commandFailed.connect(self._on_command_failed)

        self.session_sidebar.sessionChosen.connect(c.select_session)
        self.session_sidebar.newSessionRequested.connect(self._new_session)
        self.conversation.sendRequested.connect(self._send_run)
        self.conversation.stopRequested.connect(self._stop_run)
        self.approval_drawer.approved.connect(
            lambda: self._decide_changeset(True))
        self.approval_drawer.rejected.connect(
            lambda: self._decide_changeset(False))
        self.context_bar.sidebarToggled.connect(
            self.session_sidebar.setVisible)
        self.context_bar.inspectorToggled.connect(self.inspector.setVisible)

    # -- backend auto-start ----------------------------------------------

    def _start_backend_and_connect(self) -> None:
        try:
            state_dir = runtime_state_dir()
        except Exception as exc:  # bounded: misconfigured EEE_RUNTIME_HOME
            self.conversation.append_item(
                view_models.notice_card(
                    f"Runtime home is unavailable: {exc}", tone="error"))
            return
        repo_root = Path(os.environ.get("EEE_PATH", ".")).resolve()
        result = backend_launcher.ensure_runtime(repo_root, state_dir)
        if result.status in {"ready", "spawned"}:
            self._spawned_process = (
                result.process if result.status == "spawned" else None)
            self._client.start()
            return
        detail = result.detail
        if result.log_path is not None:
            detail += f" Log: {result.log_path}"
        self.conversation.append_item(
            view_models.notice_card(
                f"Runtime backend did not start ({result.status}). {detail}",
                tone="error"))

    # -- client signal handlers -------------------------------------------

    def _on_connection(self, state: str, message: str) -> None:
        self._connection = state
        self._refresh_context_bar()

    def _on_sessions(self, sessions, selected_id: str) -> None:
        self.session_sidebar.set_sessions(sessions, selected_id)

    def _on_session(self, session_id: str, title: str, cursor: int) -> None:
        self._session_title = title
        self._refresh_context_bar()

    def _on_snapshot(self, snapshot) -> None:
        if snapshot:
            state = snapshot.get("run", {}).get("state")
            if type(state) is str:
                self._run_state = state
                self.conversation.set_composer_state(
                    "idle" if state in {"Completed", "Cancelled", "Failed"}
                    else "running")
        self.inspector.set_run_snapshot(snapshot.get("run") if snapshot else None)
        self._refresh_context_bar()

    def _on_changesets(self, changesets) -> None:
        for summary in changesets:
            if summary.get("state") == "AwaitingApproval":
                self.approval_drawer.show_changeset(summary)
                return
        self.approval_drawer.hide_drawer()

    def _on_artifact(self, summary) -> None:
        self.conversation.append_item(view_models.artifact_card(summary))

    def _on_vision(self, summary) -> None:
        self.conversation.append_item(view_models.vision_card(summary))

    def _on_command_failed(self, purpose, code, message, retryable, fatal):
        self.conversation.append_item(
            view_models.notice_card(f"{purpose} failed: {message}",
                                    tone="error"))

    # -- user intents -------------------------------------------------------

    def _send_run(self, text: str) -> None:
        self.conversation.append_item(view_models.user_message(text))
        self._client.start_run(text)
        self.conversation.set_composer_state("running")

    def _stop_run(self) -> None:
        self.conversation.set_composer_state("stopping")
        self._client.stop_run("", force=False)  # client tracks the active run

    def _decide_changeset(self, approve: bool) -> None:
        self.approval_drawer.hide_drawer()
        self._client.decide_changeset(None, approve=approve)

    def _new_session(self) -> None:
        from houdini_side.runtime_panel.client import SessionTitleDialog

        dialog = SessionTitleDialog(self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self._client.create_session(dialog.title())

    # -- responsive layout ---------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        width = self.width()
        self.inspector.setVisible(width >= _WIDE_MIN_WIDTH)
        self.session_sidebar.setVisible(width >= _MEDIUM_MIN_WIDTH)
        self.context_bar.inspector_button.setChecked(width >= _WIDE_MIN_WIDTH)
        self.context_bar.sidebar_button.setChecked(width >= _MEDIUM_MIN_WIDTH)

    def _refresh_context_bar(self) -> None:
        hip = "-"
        try:
            import hou  # type: ignore

            hip = hou.hipFile.basename() or "untitled.hip"
        except Exception:
            pass
        self.context_bar.set_status(view_models.context_status(
            hip=hip, session_title=self._session_title,
            workspace_id=self._workspace_id, connection=self._connection,
            bridge=self._bridge, run_state=self._run_state,
        ))

    # -- teardown -------------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self._spawned_process is not None:
            backend_launcher.terminate(self._spawned_process)
            self._spawned_process = None
        self._client.stop()
        super().closeEvent(event)
```

Before Step 4, reconcile two call sites against the real client API in `client.py` (moved from the legacy file — check the actual signatures there):

- `stop_run(run_id, *, force)` takes the run id; the legacy panel tracks the active run id from the snapshot. Add a small `_active_run_id` field set in `_on_snapshot` and pass it instead of `""`.
- `decide_changeset(...)` in the legacy panel is called with the selected changeset's id and digest (see legacy `_decide_current_changeset`). Store the summary passed to `show_changeset` in `_on_changesets` and forward its `change_id`/`changeset_digest` exactly as the legacy code does. Do not invent a new calling convention.

- [ ] **Step 4: Rewire `__init__.py` to the new panel**

Replace the entire content of `houdini_side/runtime_panel/__init__.py` with:

```python
"""Three-pane Runtime panel package. Public pypanel contract lives here."""

from __future__ import annotations

from houdini_side.runtime_panel import theme
from houdini_side.runtime_panel.main_window import RuntimePanel


def create_panel() -> RuntimePanel:
    theme.register_fonts()
    return RuntimePanel()


def open_panel():  # kept for houdini_side.launch compatibility
    panel = create_panel()
    panel.show()
    return panel
```

Keep `legacy.py` and `client.py` untouched in this commit — `legacy.py` remains importable as a fallback reference and is removed in Task 11.

- [ ] **Step 5: Run the full panel test set**

Run: `uv run --frozen --extra eval pytest -q tests/panel -x`
Expected: all PASS. The legacy boundary test (`test_panel_package.py`) still passes because `legacy.py` is unchanged; the new source-contract tests pass against the new modules.

- [ ] **Step 6: Hython smoke check (inside Houdini's Python, manual)**

Run (Git Bash, quoting for Git Bash):

```bash
"/d/houdini/bin/hython.exe" -c "import sys; sys.path.insert(0, r'E:/eee-agent/.worktrees/runtime'); from houdini_side.runtime_panel import create_panel; print('import ok')"
```

Expected: `import ok` (no Qt widget instantiation in hython — only the import path and font registration no-op are validated here; widget behavior is verified in the GUI checklist).

- [ ] **Step 7: Commit**

```bash
git add houdini_side/runtime_panel tests/panel/test_runtime_panel_sources.py
git commit -m "feat: assemble three-pane runtime panel with backend auto-start"
```

#### Integration notes applied during execution

1. **Approval drawer geometry:** `_position_drawer()` right-anchors the
   drawer inside the conversation pane (width 320, margin 8, full height),
   called from `resizeEvent`, from an event filter on the conversation's
   `Resize` events (splitter drags), and after every `show_changeset`;
   `raise_()` after each positioning.
2. **Blocking launcher off the UI thread:** `ensure_runtime` runs on a
   daemon `threading.Thread`; a 100 ms `QTimer` polls a `threading.Event`
   and applies the result on the UI thread, only then calling
   `self._client.start()`. The context bar shows `connecting` while the
   launch is in flight. A `timeout` result's still-running process is kept
   in `_spawned_process` so `closeEvent` reaps it.
3. **stop_run:** `_active_run_id` is tracked from `snapshot["active_run"]`
   in `_on_snapshot` (mirroring legacy `_set_runtime_snapshot`) and passed
   to `self._client.stop_run(run_id, force=False)`; no-op when no active Run.
4. **decide_changeset:** the summary handed to `show_changeset` is stored
   in `_pending_changeset`; `_decide_changeset` re-checks
   `approval_is_actionable` and forwards `summary["change_id"]` /
   `summary["changeset_digest"]` exactly like legacy
   `_decide_current_changeset`.
5. **Inspector key shapes:** `set_run_snapshot` receives the active (else
   selected) run mapping directly; artifact/vision summaries from
   `artifactObserved`/`visionObserved` are accumulated with
   `append_artifact_summary` / `append_vision_summary` and passed to
   `render_artifacts` / `render_visions` unadapted.
6. **Snapshot shape:** `_on_snapshot` reads `active_run` / `selected_run`
   (never a flat `run` key), matching `RuntimePanelState.snapshot()`.
7. **Extra reconciliations found while wiring:**
   - `view_models.vision_card` consumed `vision_status`/`decision` keys
     that `parse_vision_event` never emits; it now reads the real
     `status`/`accepted`/`report_summary` shape (test updated).
   - `__init__.py` keeps `main_window` (PySide6) lazily imported — an
     eager import broke Qt-free panel tests in the venv without PySide6.
     The literal `main_window` import line is preserved inside
     `create_panel` / `__getattr__`.
   - `SessionTitleDialog` is invoked via `exec_()` because the source
     boundary test forbids the plain builtin-named call.
   - A failed `run.start` command resets the composer to `idle` so the
     optimistic `running` state cannot wedge when no Session is active.

8. **Review follow-up fixes (commit "fix: close offline composer wedge, …"):**
   - Offline send gate: `_send_run` / `_stop_run` require
     `self._connection == "online"` because `client._send` silently drops
     commands while offline; otherwise an error notice card is appended
     and the composer returns to `idle` (no more `running`/`stopping`
     wedge).
   - Mid-launch close reap: `self._closing` (`threading.Event`) is set
     first in `closeEvent`; `_launch_worker` terminates a spawned process
     itself when the panel closed before the poll, and `closeEvent` reaps
     an unpolled result-box process to close the remaining race.
   - Session switch reset: `_current_session_id` is tracked; a genuinely
     different session id clears the artifact/vision tuples, re-renders
     the inspector, and appends a "Switched to session …" notice card.
   - Bridge state: `SelectionQueryWorker` is wired like legacy
     (`queryStarted`→connecting, `querySucceeded`→ready,
     `queryFailed`→unavailable), refreshed on connect and once at startup
     via `QTimer.singleShot`; selection rows stay out of scope.
   - Workspace flows: `commandSucceeded` handles `workspace.*` purposes —
     `_workspace_id` from `result["workspace"]["workspace_id"]`, facts to
     `inspector.set_workspace_facts`; the WORKSPACE tab gained
     "Create workspace" / "Inspect" buttons (signals on `InspectorPane`,
     client calls in the panel, mirroring legacy affordances).
   - Expired changesets: `_on_changesets` only opens the drawer for
     `approval_is_actionable` summaries; an expired `AwaitingApproval`
     gets one `approval_result_card(expired=True)` per change_id.
   - Force stop reachable: composer state `stopping-forceable` (button
     stays enabled, text "Force stop") when the active run is
     `StopRequested`/`Stopping`; `_stop_run` then calls
     `stop_run(run_id, force=True)` (legacy `_update_run_controls`).

---

### Task 11: Remove legacy panel, final gates, GUI checklist handoff

**Files:**
- Delete: `houdini_side/runtime_panel/legacy.py`
- Modify: `tests/panel/test_panel_package.py` (retarget the boundary contract at the package)
- Modify: `docs/handoffs/2026-07-20-runtime-development-transfer.md` (note the UI rebuild status)

- [ ] **Step 1: Rewrite the legacy boundary test for the package**

Replace `tests/panel/test_panel_package.py` with a package-level contract:

```python
"""Package boundary contract for the three-pane Runtime panel."""

from __future__ import annotations

import ast
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "houdini_side" / "runtime_panel"

FORBIDDEN_IMPORTS = {
    "sqlite3", "aiosqlite", "rpyc", "hrpyc",
    "eee_agent.app", "eee_agent.runtime.database",
    "eee_agent.runtime.checkpoints", "eee_agent.changesets.service",
    "eee_agent.houdini_bridge.changeset_provider",
}
FORBIDDEN_TEXT = ("changeset.apply", "hou.selectedNodes", "asyncio.run",
                  "eval(", "exec(")


def test_pypanel_declares_one_menu_visible_runtime_interface() -> None:
    path = ROOT / "python_panels" / "EEEAgentRuntime.pypanel"
    root = ElementTree.parse(path).getroot()
    interfaces = root.findall("interface")
    assert len(interfaces) == 1
    interface = interfaces[0]
    assert interface.attrib["name"] == "eee_agent_runtime"
    assert interface.attrib["label"] == "EEE Runtime"
    assert interface.find("includeInPaneTabMenu") is not None
    script = interface.findtext("script") or ""
    assert "runtime_panel.create_panel()" in script
    assert "onDestroyInterface" in script


def test_package_has_no_legacy_module() -> None:
    assert not (PKG / "legacy.py").exists()


def test_all_modules_keep_import_boundary() -> None:
    for path in PKG.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert imported.isdisjoint(FORBIDDEN_IMPORTS), path.name
        text = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_TEXT:
            assert forbidden not in text, f"{path.name}: {forbidden}"


def test_client_keeps_ime_and_security_wiring() -> None:
    source = (PKG / "client.py").read_text(encoding="utf-8")
    assert "WA_InputMethodEnabled" in source
    assert "SessionTitleDialog" in source
    assert "RunRequestEdit" in source
    assert "QInputDialog.getText" not in source
    assert "textMessageReceived.connect(self._on_text_message)" in source
    assert "binaryMessageReceived.connect(self._on_binary_message)" in source
```

- [ ] **Step 2: Delete legacy.py and run the panel tests**

```bash
git rm houdini_side/runtime_panel/legacy.py
uv run --frozen --extra eval pytest -q tests/panel -x
```

Expected: all PASS. If `client.py` still imports anything from `legacy`, move that definition into `client.py` first — `client.py` must not import from `legacy`.

- [ ] **Step 3: Run the full repository gates**

```bash
uv run --frozen --extra eval pytest -q
uv run --frozen --group dev ruff check eee_agent/runtime eee_agent/vision houdini_side tests/panel
uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
uv lock --check
git diff --check
```

Expected: full suite green (baseline at the branch head: ~2900 passed, 11 skipped); ruff/compileall/lock/diff clean. The 11 Houdini Knowledge skips remain the explicit contract (do not "fix" them).

- [ ] **Step 4: Update the handoff doc**

Append to `docs/handoffs/2026-07-20-runtime-development-transfer.md` under "Manual gates and local Houdini state":

```markdown
- 2026-07-21: the panel view layer was rebuilt as the three-pane
  conversation-centric layout (spec
  `docs/superpowers/specs/2026-07-21-runtime-panel-three-pane-design.md`).
  The interactive GUI checklist must now run against this UI; the checklist
  items are unchanged. Backend startup is automatic (panel spawns
  `python -m eee_agent.runtime serve` when discovery is missing) — verify
  the auto-start path as part of the checklist's reconnect/restart item.
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: remove legacy panel; finalize three-pane redesign"
```

- [ ] **Step 6: Manual GUI checklist (human, in Houdini 21)**

Run the S9 interactive GUI checklist against the new UI in the visible Houdini FX 21.0.440 session: narrow/docked layout (verify both drawer thresholds at 900px/700px), focus, Chinese IME in the composer and session dialog, review/approval mouse flow via the drawer, reconnect/restart (kill the backend; verify the offline card and retry; verify panel-spawned backend is reaped on panel close), artifacts, recovery, and the full MODEL → REVIEW → Approve and build → RESULT journey. Evidence stays machine-local.

---

## Self-review notes

- **Spec coverage:** layout/drawers → Tasks 5, 10; conversation + honesty rule → Tasks 3, 7; approval drawer → Task 8; inspector → Task 9; theme/tokens/fonts/icons → Task 2; backend auto-start + ownership + three offline states → Tasks 4, 10; state layer untouched → no task modifies `eee_agent/panel/*`; testing strategy (source-boundary + Qt-free units + manual checklist) → Tasks 2–4, 5–10 test files, Task 11; acceptance criteria 1–6 → Tasks 10, 11.
- **Deviation from spec (agreed during planning):** composer stays single-line `RunRequestEdit` for IME safety (multiline deferred); Qt tests are source-boundary instead of offscreen because PySide6 is not a project dependency. Both deviations were edited into the spec before this plan was written.
- **Type consistency:** `MessageItem`/`ContextStatus` fields match between `view_models.py` (Task 3) and consumers (Tasks 5, 7, 10); `LaunchResult.status` values match between `backend_launcher.py` (Task 4) and `main_window.py` (Task 10); `ensure_runtime`/`terminate` names match; `discovery_ready` used consistently.
