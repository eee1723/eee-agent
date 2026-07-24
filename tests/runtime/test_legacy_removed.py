from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from eee_agent.app import build_agent
from eee_agent.cli import build_parser
from eee_agent.runtime.agent_tools import build_read_only_tools
from eee_agent.system_prompt import build_system_prompt

ROOT = Path(__file__).resolve().parents[2]
LEGACY_PACKAGE_PREFIXES = ("eee_agent/bridge/", "eee_agent/tools/")


def _tracked_paths(ref: str = "HEAD") -> set[str]:
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line}


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
    # propose_modeling was retired in Phase 3.1 in favor of the sandbox workflow.
    assert "propose_modeling" not in prompt
    for current in (
        "scene_status", "query_scene", "inspect_workspace", "geometry_stats",
        "work_status",
        # The new iterative modeling workflow: sandbox build + commit.
        "scratch_build", "scratch_commit", "沙箱",
        # Always reply in Chinese (the user-facing language directive).
        "始终用中文回复",
    ):
        assert current in prompt


def test_runtime_import_isolated_from_legacy_bridge_and_tools() -> None:
    script = """
import importlib.util, sys
import eee_agent.runtime.agent_runner
if any(name == 'eee_agent.bridge' or name.startswith('eee_agent.bridge.')
       or name == 'eee_agent.tools' or name.startswith('eee_agent.tools.')
       for name in sys.modules):
    raise SystemExit('legacy module imported')
if importlib.util.find_spec('eee_agent.workflow_middleware') is not None:
    raise SystemExit('tombstone workflow middleware reintroduced')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=os.environ.copy(),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_deleted_legacy_packages_are_not_tracked_or_discoverable() -> None:
    tracked = _tracked_paths()
    for prefix in LEGACY_PACKAGE_PREFIXES:
        assert not any(path.startswith(prefix) for path in tracked), (
            f"legacy package was re-tracked under HEAD: {prefix}"
        )

    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    script = """
import importlib
import importlib.util
import sys

sys.path.insert(0, sys.argv[1])
importlib.invalidate_caches()
import eee_agent
for name in ('eee_agent.bridge', 'eee_agent.tools'):
    spec = importlib.util.find_spec(name)
    if spec is not None:
        raise SystemExit(name + ' unexpectedly discoverable: ' + repr(spec))
    try:
        importlib.import_module(name)
    except ModuleNotFoundError:
        pass
    else:
        raise SystemExit(name + ' unexpectedly importable')
"""
    with tempfile.TemporaryDirectory(prefix="eee-legacy-archive-") as temp_root:
        archive_root = Path(temp_root)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
            source.extractall(archive_root)
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", script, str(archive_root)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize("removed", ["selftest", "prompt", "stdio"])
def test_cli_parser_rejects_removed_modes(removed: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([removed])


def test_cli_parser_retains_versions_only() -> None:
    assert build_parser().parse_args(["versions"]).mode == "versions"


def test_runtime_config_has_no_legacy_rpc_host_port_surface() -> None:
    import eee_agent.config as config

    assert not hasattr(config, "rpc_config")
    assert not hasattr(config, "RpcConfig")
