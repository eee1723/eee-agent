"""HFS discovery and hython SOP node-type inventory.

This is the only Stage 3 module allowed to inspect HFS paths or invoke
subprocesses. ``hou`` is imported only inside the hython child program string
(which runs in the child process); the parent process never imports ``hou``.
No absolute HFS path is returned through the inventory (only operator names and
the version/build strings).

``load_inventory_snapshot`` makes a single hython call that returns the Houdini
version, build and the SOP node-type inventory as one strict JSON object.
``load_sop_inventory`` is a thin compatibility facade that returns only the
operator set.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

__all__ = [
    "HfsResolutionError",
    "HythonInventoryError",
    "HythonInventorySnapshot",
    "InventoryError",
    "load_inventory_snapshot",
    "load_sop_inventory",
    "resolve_hfs",
]

_REQUIRED_ARCHIVES = ("nodes.zip", "hom.zip", "vex.zip")

# Executed only inside the child hython process (never imported by the parent).
# Emits one strict JSON object: the integer version tuple, the version/build
# string, and the sorted unique SOP node-type names. ``hou.applicationVersion``
# returns the (major, minor, build) integers and ``hou.applicationVersionString``
# the dotted string; both are verified against Houdini 21.0.440.
_HYTHON_SNAPSHOT_PROGRAM = (
    "import hou, json\n"
    "major, minor, build = hou.applicationVersion()\n"
    "names = sorted(set(t.name() for t in "
    "hou.sopNodeTypeCategory().nodeTypes().values()))\n"
    "print(json.dumps({"
    "'version': [int(major), int(minor), int(build)], "
    "'build': hou.applicationVersionString(), "
    "'sop_node_types': names}))\n"
)


class InventoryError(Exception):
    """Base class for HFS discovery and hython inventory failures."""


class HfsResolutionError(InventoryError):
    """The HFS root could not be resolved or failed validation."""


class HythonInventoryError(InventoryError):
    """The hython inventory subprocess failed or returned invalid output."""


@dataclass(frozen=True, slots=True)
class HythonInventorySnapshot:
    """The version/build and SOP node-type inventory from one hython call.

    ``houdini_version`` is derived from the integer version tuple and
    ``houdini_build`` from the dotted build string; the loader validates they
    agree. ``sop_node_types`` is a deduplicated set of operator names only.
    """

    houdini_version: str
    houdini_build: str
    sop_node_types: frozenset[str]


def _is_valid_hfs(path: Path) -> bool:
    if not path.is_dir():
        return False
    help_dir = path / "houdini" / "help"
    for archive in _REQUIRED_ARCHIVES:
        if not (help_dir / archive).is_file():
            return False
    return (path / "bin" / "hython.exe").is_file()


def resolve_hfs(
    explicit: str | None,
    environ: Mapping[str, str],
    candidates: tuple[Path, ...],
) -> Path:
    """Resolve the HFS root with priority: --hfs > EEE_HFS > HFS > candidates.

    An explicitly configured source (``--hfs``, ``EEE_HFS`` or ``HFS``) is
    authoritative: if it is present but invalid a :class:`HfsResolutionError`
    is raised rather than silently falling through, so a typo is not hidden.
    Known-path candidates are tried in supplied order; the first valid one
    wins. ``os.environ`` is never read -- the environment mapping is injected.
    """
    provided: list[str] = []
    if explicit is not None:
        provided.append(explicit)
    if environ.get("EEE_HFS"):
        provided.append(environ["EEE_HFS"])
    if environ.get("HFS"):
        provided.append(environ["HFS"])

    if provided:
        path = Path(provided[0])
        if not _is_valid_hfs(path):
            raise HfsResolutionError(
                "configured HFS path is not a valid HFS root"
            )
        return path

    for candidate in candidates:
        path = Path(candidate)
        if _is_valid_hfs(path):
            return path
    raise HfsResolutionError("no valid HFS root found in candidates")


def _has_control_char(text: str) -> bool:
    return any(ord(ch) <= 0x1F or ord(ch) == 0x7F for ch in text)


def _parse_snapshot_stdout(stdout: str) -> HythonInventorySnapshot:
    text = (stdout or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HythonInventoryError(
            f"malformed hython snapshot JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise HythonInventoryError("hython snapshot output must be a JSON object")

    version = data.get("version")
    if not (
        isinstance(version, list)
        and len(version) == 3
        and all(isinstance(part, int) and not isinstance(part, bool) for part in version)
    ):
        raise HythonInventoryError(
            "hython snapshot 'version' must be a list of three integers"
        )

    build = data.get("build")
    if not isinstance(build, str) or not build:
        raise HythonInventoryError(
            "hython snapshot 'build' must be a non-empty string"
        )
    version_string = f"{version[0]}.{version[1]}.{version[2]}"
    if build != version_string:
        raise HythonInventoryError(
            "hython snapshot 'build' is inconsistent with 'version'"
        )

    raw_types = data.get("sop_node_types")
    if not isinstance(raw_types, list) or not raw_types:
        raise HythonInventoryError(
            "hython snapshot 'sop_node_types' must be a non-empty array"
        )
    names: set[str] = set()
    for item in raw_types:
        if not isinstance(item, str):
            raise HythonInventoryError(
                "hython snapshot 'sop_node_types' member is not a string"
            )
        name = item.strip()
        if not name:
            raise HythonInventoryError(
                "hython snapshot 'sop_node_types' contains an empty name"
            )
        if _has_control_char(name):
            raise HythonInventoryError(
                "hython snapshot 'sop_node_types' contains a control character"
            )
        names.add(name)

    return HythonInventorySnapshot(
        houdini_version=version_string,
        houdini_build=build,
        sop_node_types=frozenset(names),
    )


def load_inventory_snapshot(
    hfs: Path,
    *,
    runner=subprocess.run,
    timeout_seconds: int = 30,
) -> HythonInventorySnapshot:
    """Return the Houdini version/build and SOP node-type inventory.

    Hython is invoked once with an argument list (never a shell string) and the
    given ``runner`` (``subprocess.run`` by default; tests inject a fake). A
    timeout, non-zero exit, malformed JSON, a non-object payload, a version
    that is not three integers, an empty/inconsistent build, an empty
    ``sop_node_types`` array, a non-string/empty/control-bearing member all raise
    :class:`HythonInventoryError`. On success the child's stdout is strict JSON;
    stderr may be captured but is not parsed.
    """
    hython = Path(hfs) / "bin" / "hython.exe"
    if not hython.is_file():
        raise HythonInventoryError("hython executable not found in HFS")
    args = [str(hython), "-c", _HYTHON_SNAPSHOT_PROGRAM]
    try:
        result = runner(
            args,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise HythonInventoryError(
            f"hython inventory timed out after {timeout_seconds}s"
        ) from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise HythonInventoryError(
            f"hython exited with code {result.returncode}: {stderr[:200]}"
        )
    return _parse_snapshot_stdout(result.stdout)


def load_sop_inventory(
    hfs: Path,
    *,
    runner=subprocess.run,
    timeout_seconds: int = 30,
) -> frozenset[str]:
    """Return the SOP operator type names (compatibility facade).

    Reuses :func:`load_inventory_snapshot` and returns only ``sop_node_types``.
    """
    return load_inventory_snapshot(
        hfs, runner=runner, timeout_seconds=timeout_seconds
    ).sop_node_types
