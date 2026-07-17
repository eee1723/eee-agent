from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

from eee_agent.app import build_agent
from eee_agent.cli import build_parser
from eee_agent.runtime.agent_tools import build_read_only_tools
from eee_agent.system_prompt import build_system_prompt

ROOT = Path(__file__).resolve().parents[2]


def test_formal_menu_has_no_legacy_raw_write_entries() -> None:
    text = (ROOT / "MainMenuCommon.xml").read_text(encoding="utf-8")
    assert "Open Agent Panel" not in text
    assert "Start RPC Bridge Only" not in text
    assert "start_rpc" not in text
    assert "chat_panel" not in text
    root = ElementTree.fromstring(text)
    ids = {item.attrib.get("id") for item in root.findall(".//scriptItem")}
    assert "eee_start_rpc" not in ids


def test_build_agent_requires_explicit_tools() -> None:
    with pytest.raises(TypeError, match="explicit secure tools"):
        build_agent()


def test_runtime_tools_are_read_only_and_do_not_load_legacy_modules() -> None:
    names = {item.name for item in build_read_only_tools()}
    assert not names & {
        "create_node", "set_parms", "delete_node", "scene_reset", "cook_node",
        "export_geometry", "set_vex", "connect_nodes",
    }


def test_runtime_system_prompt_describes_only_current_capabilities() -> None:
    prompt = build_system_prompt()
    for removed in (
        "create_node", "connect_nodes", "set_parms", "set_vex", "scene_reset",
        "cook_node", "export_geometry", "save_hip", "ensure_work_container",
        "add_root_parm", "make_component", "anchor_graph",
    ):
        assert removed not in prompt
    for current in (
        "scene_status", "query_scene", "inspect_workspace", "geometry_stats",
        "work_status", "typed proposal", "explicitly approve",
    ):
        assert current in prompt


def test_runtime_import_isolated_from_legacy_bridge_and_tools() -> None:
    script = """
import os, sys
os.environ['EEE_WORKFLOW_STATUS'] = 'true'
import eee_agent.runtime.agent_runner
import eee_agent.workflow_middleware
if any(name == 'eee_agent.bridge' or name.startswith('eee_agent.bridge.')
       or name == 'eee_agent.tools' or name.startswith('eee_agent.tools.')
       for name in sys.modules):
    raise SystemExit('legacy module imported')
if eee_agent.workflow_middleware.is_enabled():
    raise SystemExit('legacy workflow middleware enabled')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=os.environ.copy(),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_deleted_legacy_packages_are_not_discoverable() -> None:
    script = """
import importlib.util
for name in ('eee_agent.bridge', 'eee_agent.tools'):
    spec = importlib.util.find_spec(name)
    if spec is not None and spec.origin is not None:
        raise SystemExit(name + ' unexpectedly has a source origin: ' + spec.origin)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=os.environ.copy(),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize("removed", ["selftest", "prompt", "stdio"])
def test_cli_parser_rejects_removed_modes(removed: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([removed])


def test_cli_parser_retains_versions_only() -> None:
    assert build_parser().parse_args(["versions"]).mode == "versions"
