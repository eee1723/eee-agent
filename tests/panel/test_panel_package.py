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
    assert "SessionTitleDialog" not in source
    assert "RunRequestEdit" in source
    assert "QInputDialog.getText" not in source
    assert "textMessageReceived.connect(self._on_text_message)" in source
    assert "binaryMessageReceived.connect(self._on_binary_message)" in source


def test_composer_is_multiline_with_ctrl_enter_submit() -> None:
    source = (PKG / "client.py").read_text(encoding="utf-8")
    # The composer is a QPlainTextEdit (multi-line) with IME multiline mode,
    # and Ctrl+Enter is the explicit submit gesture (bare Enter inserts a
    # newline so IME candidate confirmation never fires a Run).
    assert "class RunRequestEdit(QtWidgets.QPlainTextEdit)" in source
    assert "_configure_ime(self, multiline=True)" in source
    assert "submitRequested = QtCore.Signal()" in source
    assert "ControlModifier" in source
    assert "returnPressed" not in source  # never wired on the composer


def test_client_auto_creates_session_when_none_active() -> None:
    # Sending a prompt with no active Session auto-creates one (placeholder
    # title) and stashes the prompt to fire run.start once it activates.
    source = (PKG / "client.py").read_text(encoding="utf-8")
    assert "_pending_run_input" in source
    assert '"New session"' in source
    assert "self._pending_run_input = user_input" in source
    # The stashed prompt fires when the new Session activates post-reconnect.
    assert "self._pending_run_input is not None" in source
    # A failed auto-create clears the stash so it isn't silently swallowed.
    assert 'purpose == "session.create"' in source
    assert "self._pending_run_input = None" in source


def test_new_session_is_unnamed_and_coalesced() -> None:
    client_source = (PKG / "client.py").read_text(encoding="utf-8")
    window_source = (PKG / "main_window.py").read_text(encoding="utf-8")
    sidebar_source = (PKG / "session_sidebar.py").read_text(encoding="utf-8")
    assert "_new_session" not in window_source
    assert "def create_unnamed_session" in client_source
    assert "_session_create_inflight" in client_source
    assert "New session" in client_source
    assert "未命名对话" in sidebar_source


def test_create_unnamed_session_signals_when_already_on_placeholder() -> None:
    # Idempotency is preserved (no duplicate create sent), but the client must
    # emit a signal so the panel can give feedback instead of appearing dead.
    source = (PKG / "client.py").read_text(encoding="utf-8")
    assert "emptySessionFocused" in source


def test_first_connection_defaults_to_empty_session() -> None:
    # A freshly opened panel must not restore the last-used Session. It selects
    # (or creates) the empty placeholder on the first session.list, ignoring the
    # persisted preferred id. Subsequent reconnects still honor the preferred id
    # so an in-progress conversation is preserved across reconnect.
    source = (PKG / "client.py").read_text(encoding="utf-8")
    assert "_bootstrap_complete" in source
    # The persisted preferred id is still loaded (used for reconnect), but the
    # first session.list resolution must NOT pass it as the preferred choice.
    assert "_load_preferred_session_id()" in source


def test_client_exposes_changeset_recover_command() -> None:
    # The manual CriticalRecovery exit must be reachable from the panel client.
    source = (PKG / "client.py").read_text(encoding="utf-8")
    assert "def recover_changeset" in source
    assert '"changeset.recover"' in source


def test_auto_execute_preference_is_persisted_and_wired() -> None:
    # The auto-execute toggle is a persisted QSettings preference (off by
    # default) that drives the _on_changesets auto-approve path.
    client_source = (PKG / "client.py").read_text(encoding="utf-8")
    assert "_AUTO_EXECUTE_KEY" in client_source
    assert "def _load_auto_execute" in client_source
    assert "def _save_auto_execute" in client_source
    assert "def is_auto_execute" in client_source
    assert "def set_auto_execute" in client_source
    window_source = (PKG / "main_window.py").read_text(encoding="utf-8")
    assert "is_auto_execute()" in window_source
    context_source = (PKG / "context_bar.py").read_text(encoding="utf-8")
    assert "autoExecuteToggled" in context_source


def test_client_refreshes_sidebar_on_session_renamed() -> None:
    # An auto-titled Session emits session.renamed; the client must update the
    # cached title and re-emit sessionsChanged so the sidebar refreshes in
    # place (no extra session.list round-trip).
    source = (PKG / "client.py").read_text(encoding="utf-8")
    assert '"session.renamed"' in source
    assert "def _apply_renamed_session" in source
    assert 'cached["title"] = title' in source
    assert "self.sessionsChanged.emit" in source
