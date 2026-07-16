from __future__ import annotations

import ast
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
    path = ROOT / "houdini_side" / "runtime_panel.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
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
        "run.start",
        "workspace.create",
        "eval(",
        "exec(",
    ):
        assert forbidden_text not in source


def test_legacy_chat_panel_remains_present_as_rollback() -> None:
    assert (ROOT / "houdini_side" / "chat_panel.py").is_file()
