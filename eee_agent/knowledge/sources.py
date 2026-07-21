"""Safe in-memory loading of Houdini source archives and project skills.

Zip entries are never extracted to disk: archives are parsed from bytes through
an in-memory file object and each entry name is validated with the same logical
POSIX rule used for entity source paths. Unsafe names (absolute, UNC, drive,
traversal, empty segments, control characters) raise :class:`SourceError`.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from eee_agent.knowledge.ids import normalize_source_path
from eee_agent.knowledge.manifest import SourceFingerprint, fingerprint_bytes

__all__ = [
    "REQUIRED_SKILL_PATHS",
    "SourceError",
    "load_archive_entries",
    "load_skill_sources",
]


class SourceError(Exception):
    """A source archive entry or skill path was unsafe."""


# The exact, exhaustive set of project skills the builder accepts (design §5).
# A build fails if any is missing or if any extra ``*/SKILL.md`` is present, so a
# stale glob result can never pass as the complete source set.
REQUIRED_SKILL_PATHS = (
    "parametric-building/SKILL.md",
    "procedural-components/SKILL.md",
    "sop-cookbook/SKILL.md",
    "vex-patterns/SKILL.md",
)


def load_archive_entries(data: bytes) -> list[tuple[str, bytes]]:
    """Return ``(logical_name, entry_bytes)`` for each safe entry of a zip.

    The zip is parsed from ``data`` in memory and never extracted to disk.
    Directory entries are skipped; unsafe entry names raise
    :class:`SourceError`; backslash separators are normalized to forward slashes.
    """
    entries: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            try:
                logical = normalize_source_path(info.filename)
            except ValueError as exc:
                raise SourceError(f"unsafe zip entry name: {info.filename!r}") from exc
            entries.append((logical, zf.read(info)))
    return entries


def load_skill_sources(
    skills_dir: Path, repo_root: Path
) -> tuple[tuple[SourceFingerprint, ...], list[tuple[str, bytes]]]:
    """Load exactly the four required project skills under ``skills_dir``.

    Returns ``(fingerprints, entries)`` where each logical path is the SKILL.md
    path relative to ``repo_root``, normalized to POSIX form. A missing required
    skill or any extra ``*/SKILL.md`` raises :class:`SourceError` -- the result
    is never a silent partial glob.
    """
    skills_dir = Path(skills_dir)
    repo_root = Path(repo_root)
    fingerprints: list[SourceFingerprint] = []
    entries: list[tuple[str, bytes]] = []
    required = set(REQUIRED_SKILL_PATHS)
    for relative in REQUIRED_SKILL_PATHS:
        skill_path = skills_dir / relative
        if not skill_path.is_file():
            raise SourceError(f"missing required project skill: skills/{relative}")
        data = skill_path.read_bytes()
        logical = normalize_source_path(str(skill_path.relative_to(repo_root)))
        fingerprints.append(fingerprint_bytes(logical, data))
        entries.append((logical, data))
    # Reject any unexpected top-level SKILL.md so the source set is exact.
    for candidate in sorted(skills_dir.glob("*/SKILL.md")):
        if candidate.relative_to(skills_dir).as_posix() not in required:
            rel = candidate.relative_to(skills_dir).as_posix()
            raise SourceError(f"unexpected project skill: skills/{rel}")
    return tuple(fingerprints), entries
