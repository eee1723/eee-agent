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
        # theme.py owns all tokens; client.py still carries the legacy
        # SessionTitleDialog palette (the graphite/cyan source the theme
        # tokens now track), every other module must reference colors via
        # theme.
        if path.name in {"theme.py", "client.py"}:
            continue
        for match in re.finditer(r"#[0-9a-fA-F]{6}\b", path.read_text("utf-8")):
            raise AssertionError(f"hex color {match.group()} in {path.name}")


def test_session_sidebar_contract() -> None:
    source = _source("session_sidebar.py")
    assert "sessionChosen = QtCore.Signal(str)" in source
    assert "newSessionRequested = QtCore.Signal()" in source
    assert "def set_sessions(self, sessions" in source
    # Session naming is delegated to the main window via newSessionRequested;
    # the sidebar itself does not open the SessionTitleDialog.
    assert "SessionTitleDialog" not in source
    assert "def __init__(self, parent" in source  # no client param


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
    # Multi-line composer: Ctrl+Enter sends via the editor's submitRequested
    # signal (the modifier handling lives in RunRequestEdit / client.py).
    assert "submitRequested" in source
    assert "self.input.submitRequested.connect" in source
    # Streaming + kind-based rendering: cards branch on item.kind, and an
    # in-flight assistant reply updates in place instead of appending copies.
    assert 'item.kind == "user"' in source
    assert "def update_body(self, text" in source
    assert "def update_streaming(self, text" in source
    assert "def replace_last_assistant(self, item" in source
    assert "stop_streaming" in source


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


def test_inspector_contract() -> None:
    source = _source("inspector.py")
    for tab in ('"RUN"', '"WORKSPACE"', '"ARTIFACTS"'):
        assert tab in source
    assert '"VALIDATION"' not in source  # no data channel in the Runtime protocol
    assert "def set_run_snapshot(self, snapshot, activity" in source
    assert "def set_workspace_facts(self, facts)" in source
    assert "def render_artifacts(self, summaries)" in source
    assert "def render_visions(self, summaries)" in source


def test_inspector_uses_structured_widgets_not_plain_text() -> None:
    # Stage A: the inspector must no longer be a QPlainTextEdit key:value dump.
    # It builds QFrame/QTableWidget/QFormLayout structures from view_models.
    source = _source("inspector.py")
    # The legacy helper and its plain-text view are gone.
    assert "_text_view" not in source
    assert "_dump" not in source
    assert "setPlainText" not in source
    # The new renderer builds from view_models.run_view (Qt-free, unit-tested).
    assert "from houdini_side.runtime_panel import theme, view_models as vm" in source
    assert "vm.run_view(" in source
    assert "vm.workspace_rows(" in source
    assert "vm.artifact_rows(" in source
    assert "vm.vision_rows(" in source
    # A structured Run-tab widget class replaces the key:value text view.
    assert "class _RunViewWidget" in source
    # Dependencies use a real table, not a stringified dict.
    assert "QTableWidget" in source
    # Status badge tone is derived from theme tokens, never hex literals.
    assert "STATUS_OK" in source or "theme.STATUS_OK" in source
    assert "STATUS_ERROR" in source or "theme.STATUS_ERROR" in source


def test_inspector_renders_failure_block_from_typed_failure_view() -> None:
    # Stage A / Task 4: the RUN inspector must render a structured FailureView
    # (provided by view_models.RunView.failure) instead of leaving Failed runs
    # showing only the status badge or dumping failure_json as raw text. The Qt
    # layer only consumes the typed FailureView fields; it must never read or
    # stringify the raw failure payload.
    source = _source("inspector.py")
    # update_view() gates the block on a typed FailureView, never the raw json.
    assert "if run_view.failure is not None:" in source
    assert "self._build_failure_block(run_view.failure)" in source
    assert "def _build_failure_block(self, failure" in source
    # Only the four typed FailureView fields may be read.
    assert "failure.code" in source
    assert "failure.message" in source
    assert "failure.retryable" in source
    assert "failure.tone" in source
    # A clear failure block title and a retry hint keyed on the bool.
    assert '"FAILURE"' in source or '"RUN FAILED"' in source
    assert "Retry may succeed." in source
    # The block carries a stable objectName for styling / testing.
    assert '"FailureBlock"' in source
    # The raw failure payload and technical detail must never enter the Qt layer.
    # (failure_json is read only inside view_models.run_view, not here.)
    assert "failure_json" not in source
    assert "technical_detail_ref" not in source
    assert "traceback" not in source
    assert "message_for_user" not in source  # read the normalized failure.message
    assert "repr(failure)" not in source
    assert "str(failure)" not in source
    assert "failure.__dict__" not in source


def test_inspector_renders_todolist_block() -> None:
    # D-3: the agent's deepagents TodoList plan must appear in the Run tab
    # with a per-item status marker (one row per todo).
    source = _source("inspector.py")
    assert "_build_todos_block" in source
    assert "TodoItemView" in source or "vm.TodoItemView" in source
    # Status markers for the three deepagents todo states. Pending is the
    # else branch so its literal may be absent; completed and in_progress
    # are explicit branches.
    assert '"completed"' in source or "'completed'" in source
    assert '"in_progress"' in source or "'in_progress'" in source
    # The marker glyphs for done / active / pending must all be present.
    assert "✓" in source   # completed
    assert "▶" in source   # in_progress
    assert "○" in source   # pending


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
    assert "conversation.update_streaming" in source  # streaming assistant card
    assert "conversation.replace_last_assistant" in source  # terminal settle
    assert "approval_drawer.show_changeset" in source
    assert "_closing" in source                 # mid-launch close reaps backend
    assert "SelectionQueryWorker" in source     # bridge state wiring
    assert "commandSucceeded" in source         # workspace flows
    assert "stopping-forceable" in source       # force stop reachable
    assert 'def create_panel()' not in source  # factory stays in __init__


def test_package_init_exposes_create_panel() -> None:
    source = _source("__init__.py")
    assert "from houdini_side.runtime_panel.main_window import RuntimePanel" in source
    assert "def create_panel()" in source
    assert "theme.register_fonts()" in source
