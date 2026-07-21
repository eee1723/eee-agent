from __future__ import annotations

import ast
import builtins
import symtable
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[2]


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


def test_runtime_panel_keeps_client_only_import_boundary() -> None:
    paths = [
        ROOT / "houdini_side" / "runtime_panel" / "legacy.py",
        ROOT / "houdini_side" / "runtime_panel" / "client.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    nodes = [
        node
        for path in paths
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
    ]
    imported: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {
        "sqlite3",
        "aiosqlite",
        "eee_agent.app",
        "eee_agent.runtime.database",
        "eee_agent.runtime.checkpoints",
        "eee_agent.changesets.service",
        "eee_agent.houdini_bridge.changeset_provider",
        "rpyc",
        "hrpyc",
    }
    assert imported.isdisjoint(forbidden)
    for forbidden_text in (
        "changeset.apply",
        "hou.selectedNodes",
        "asyncio.run",
        "eval(",
        "exec(",
    ):
        assert forbidden_text not in source
    assert "query_selection" in source
    assert '"run.start"' in source
    assert '"changeset.approve"' in source
    assert '"changeset.reject"' in source
    assert "textMessageReceived.connect(self._on_text_message)" in source
    assert "binaryMessageReceived.connect(self._on_binary_message)" in source
    assert "SessionTitleDialog" in source
    assert "RunRequestEdit" in source
    assert "self.run_prompt = RunRequestEdit()" in source
    assert "self.run_prompt = QtWidgets.QPlainTextEdit()" not in source
    assert "QInputDialog.getText" not in source
    assert "WA_InputMethodEnabled" in source
    assert "returnPressed.connect" not in source
    assert "button.setAutoDefault(False)" in source
    assert "QtCore.Qt.Key.Key_Return" in source
    assert "Runs start only from the Start run button" in source
    assert "QSettings" in source
    assert 'self._build_run_tab(), "MODEL"' in source
    assert 'self._build_approvals_tab(), "REVIEW"' in source
    assert 'self._build_scene_tab(), "SCENE"' in source
    assert 'self._build_workspace_tab(), "WORKSPACE"' in source
    assert "self.tabs.setTabVisible(self.scene_tab_index, False)" in source
    assert "self.tabs.setTabVisible(self.workspace_tab_index, False)" in source
    assert "_toggle_developer_details" in source
    assert 'QtWidgets.QPushButton("Approve and build")' in source
    assert 'QtWidgets.QPushButton("Review plan")' in source
    assert '"workspace.create"' in source
    assert '"workspace.bind"' in source
    assert '"workspace.inspect"' in source


def test_runtime_panel_bootstraps_snapshot_before_live_subscription() -> None:
    source = (ROOT / "houdini_side" / "runtime_panel" / "client.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    client = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "RuntimeObserverClient"
    )
    activate = next(
        node
        for node in client.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_activate_session"
    )
    activate_source = ast.get_source_segment(source, activate) or ""
    assert "session.bootstrap_snapshot" in activate_source
    assert '"session.subscribe"' not in activate_source

    handler = next(
        node
        for node in client.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_handle_response"
    )
    handler_source = ast.get_source_segment(source, handler) or ""
    bootstrap_index = handler_source.index("session.bootstrap_snapshot")
    subscribe_index = handler_source.index('"session.subscribe"', bootstrap_index)
    assert subscribe_index > bootstrap_index


def test_legacy_panel_defines_every_module_global_it_references() -> None:
    source = (ROOT / "houdini_side" / "runtime_panel" / "legacy.py").read_text(
        encoding="utf-8"
    )
    table = symtable.symtable(source, "legacy.py", "exec")
    defined = {symbol.get_name() for symbol in table.get_symbols()}
    undefined: set[str] = set()

    def scan(scope: symtable.SymbolTable) -> None:
        for symbol in scope.get_symbols():
            if (
                symbol.is_referenced()
                and symbol.is_global()
                and not symbol.is_declared_global()
            ):
                name = symbol.get_name()
                if name not in defined and not hasattr(builtins, name):
                    undefined.add(name)
        for child in scope.get_children():
            scan(child)

    for child in table.get_children():
        scan(child)
    assert not undefined, f"undefined module globals: {sorted(undefined)}"


def test_legacy_chat_panel_is_not_a_formal_runtime_entrypoint() -> None:
    text = (ROOT / "houdini_side" / "README_INSTALL.md").read_text(encoding="utf-8")
    assert "Open Agent Panel" not in text
    assert "start_rpc.start()" not in text


def test_main_menu_adds_runtime_observer_without_legacy_actions() -> None:
    root = ElementTree.parse(ROOT / "MainMenuCommon.xml").getroot()
    ids = {
        item.attrib["id"]
        for item in root.findall(".//scriptItem")
    }
    assert {"eee_open_runtime_panel", "eee_start_secure_bridge", "eee_stop_secure_bridge"}.issubset(ids)
    assert "eee_open_panel" not in ids
    assert "eee_start_rpc" not in ids
    text = (ROOT / "MainMenuCommon.xml").read_text(encoding="utf-8")
    assert "Open Runtime Control" in text
    assert "secure_bridge_host.start()" in text
    assert "runtime_panel.open_panel()" in text
    assert "start_rpc.start()" not in text
    assert "chat_panel.open_panel()" not in text
