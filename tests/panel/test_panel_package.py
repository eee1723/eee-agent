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
