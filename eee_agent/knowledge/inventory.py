"""HFS discovery and hython SOP node-type inventory.

This is the only Stage 3 module allowed to inspect HFS paths or invoke
subprocesses. ``hou`` is imported only inside the hython child program string
(which runs in the child process); the parent process never imports ``hou``.
No absolute HFS path is returned through the inventory (only operator names).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Mapping

__all__ = [
    "HfsResolutionError",
    "HythonInventoryError",
    "InventoryError",
    "load_sop_inventory",
    "resolve_hfs",
]

_REQUIRED_ARCHIVES = ("nodes.zip", "hom.zip", "vex.zip")

# Executed only inside the child hython process (never imported by the parent).
# Imports hou, enumerates SOP node types, deduplicates/sorts and prints one
# JSON array with no diagnostic prose.
_HYTHON_PROGRAM = (
    "import hou, json\n"
    "names = sorted(set(t.name() for t in "
    "hou.sopNodeTypeCategory().nodeTypes().values()))\n"
    "print(json.dumps(names))\n"
)


class InventoryError(Exception):
    """Base class for HFS discovery and hython inventory failures."""


class HfsResolutionError(InventoryError):
    """The HFS root could not be resolved or failed validation."""


class HythonInventoryError(InventoryError):
    """The hython SOP inventory subprocess failed or returned invalid output."""


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


def _parse_inventory_stdout(stdout: str) -> frozenset[str]:
    text = (stdout or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HythonInventoryError(
            f"malformed hython inventory JSON: {exc}"
        ) from exc
    if not isinstance(data, list):
        raise HythonInventoryError("hython inventory output must be a JSON array")
    names: set[str] = set()
    for item in data:
        if not isinstance(item, str):
            raise HythonInventoryError(
                "hython inventory output member is not a string"
            )
        name = item.strip()
        if not name:
            raise HythonInventoryError(
                "hython inventory output contains an empty operator type name"
            )
        if any(ord(ch) <= 0x1F or ord(ch) == 0x7F for ch in name):
            raise HythonInventoryError(
                "hython inventory output contains a control character"
            )
        names.add(name)
    return frozenset(names)


def load_sop_inventory(
    hfs: Path,
    *,
    runner=subprocess.run,
    timeout_seconds: int = 30,
) -> frozenset[str]:
    """Return the sorted, deduplicated set of SOP operator type names.

    Hython is invoked with an argument list (never a shell string) and the
    given ``runner`` (``subprocess.run`` by default; tests inject a fake).
    A timeout, non-zero exit, malformed JSON, non-list, non-string member,
    empty name or control character all raise :class:`HythonInventoryError`.
    The returned frozenset contains only operator names, never HFS paths.
    """
    hython = Path(hfs) / "bin" / "hython.exe"
    if not hython.is_file():
        raise HythonInventoryError("hython executable not found in HFS")
    args = [str(hython), "-c", _HYTHON_PROGRAM]
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
    return _parse_inventory_stdout(result.stdout)
