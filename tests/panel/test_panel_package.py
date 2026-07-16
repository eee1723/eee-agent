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
        "hou.selectedNodes",
        "asyncio.run",
        "eval(",
        "exec(",
    ):
        assert forbidden_text not in source
    assert "query_selection" in source


def test_legacy_chat_panel_remains_present_as_rollback() -> None:
    assert (ROOT / "houdini_side" / "chat_panel.py").is_file()


def test_main_menu_adds_runtime_observer_without_removing_legacy_actions() -> None:
    root = ElementTree.parse(ROOT / "MainMenuCommon.xml").getroot()
    ids = {
        item.attrib["id"]
        for item in root.findall(".//scriptItem")
    }
    assert {
        "eee_open_runtime_panel",
        "eee_start_secure_bridge",
        "eee_stop_secure_bridge",
        "eee_open_panel",
        "eee_start_rpc",
    }.issubset(ids)
    text = (ROOT / "MainMenuCommon.xml").read_text(encoding="utf-8")
    assert "secure_bridge_host.start()" in text
    assert "runtime_panel.open_panel()" in text
    assert "start_rpc.start()" in text
    assert "chat_panel.open_panel()" in text
