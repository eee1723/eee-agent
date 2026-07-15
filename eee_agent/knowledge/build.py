"""Explicit, atomic knowledge-cache builder.

Acquires a stale-aware exclusive lock, gathers sources plus the single-hython
version/build/operator snapshot, parses, assembles and validates the graph,
materializes a temp SQLite cache in the target directory, validates it, then
``os.replace``-swaps it onto the final path. Any failure preserves the previous
cache and removes only this build's temp files. ``--selftest`` builds the plan's
synthetic corpus under a temporary path with no HFS, hython or RPC.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from eee_agent.knowledge.graph import assemble_graph, validate_graph
from eee_agent.knowledge.inventory import (
    HythonInventorySnapshot,
    load_inventory_snapshot,
    resolve_hfs,
)
from eee_agent.knowledge.lock import (
    LOCK_STALE_TTL_SECONDS,
    LockError,
    acquire_build_lock,
    lock_path_for,
    release_build_lock,
)
from eee_agent.knowledge.manifest import (
    BuildManifest,
    build_manifest,
    fingerprint_bytes,
    hash_inventory,
)
from eee_agent.knowledge.models import ParsedDocument
from eee_agent.knowledge.parse_common import parse_metadata
from eee_agent.knowledge.parse_hom import parse_hom_document
from eee_agent.knowledge.parse_node import parse_node_document
from eee_agent.knowledge.parse_skill import parse_skill_document
from eee_agent.knowledge.parse_vex import parse_vex_document
from eee_agent.knowledge.schema import KB_SCHEMA_VERSION
from eee_agent.knowledge.sources import load_archive_entries, load_skill_sources
from eee_agent.knowledge.writer import validate_cache, write_cache

__all__ = [
    "BUILDER_VERSION",
    "PARSER_VERSION",
    "BuildError",
    "BuildOptions",
    "build_cache",
    "main",
    "parse_archive_documents",
]

BUILDER_VERSION = "1"
PARSER_VERSION = "1"

_HOM_TYPES = frozenset(
    {"homclass", "homfunction", "hommodule", "hompackage", "pypackage"}
)
_REQUIRED_ARCHIVES = ("nodes.zip", "hom.zip", "vex.zip")
# Verified two-machine HFS roots (design §9.1). Used only to locate the source
# for this build; never stored in the cache or manifest.
_DEFAULT_HFS_CANDIDATES = (
    Path("D:/houdini"),
    Path("C:/Program Files/Side Effects Software/Houdini 21.0.440"),
)

# Patterns scrubbed from build error messages so no absolute HFS path, UNC path,
# raw SQL or traceback is ever written to the cache, manifest, stdout or stderr.
_UNC_PATH_RE = re.compile(r"\\\\[^\s'\":<>|*?]+")
_DRIVE_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s'\":<>|*?]+")
_SQL_STATEMENT_RE = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|PRAGMA)\b[^;]*;?",
    re.IGNORECASE,
)
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\):.*", re.DOTALL)


class BuildError(Exception):
    """A build could not be completed (lock, source, graph or write failure)."""


@dataclass(frozen=True, slots=True)
class BuildOptions:
    output: Path
    hfs: Path | None = None
    selftest: bool = False


def _production_now() -> datetime:
    return datetime.now(timezone.utc)


def _production_pid_exists(pid: int) -> bool:
    """Best-effort live-PID check (Windows OpenProcess)."""
    if pid <= 0:
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        # Conservative: if the probe itself fails, never reclaim a lock.
        return True


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sanitize_error(exc: BaseException) -> str:
    """Return a diagnosable, non-sensitive message for an exception.

    Scrubs Windows drive paths, UNC paths, raw SQL statements and tracebacks so
    the builder never writes an absolute HFS path, raw SQL or a traceback into
    the cache, manifest, stdout or stderr. The short remaining message (or the
    exception class name) stays diagnosable.
    """
    text = str(exc) or type(exc).__name__
    text = _UNC_PATH_RE.sub("[path]", text)
    text = _DRIVE_PATH_RE.sub("[path]", text)
    text = _SQL_STATEMENT_RE.sub("[sql]", text)
    text = _TRACEBACK_RE.sub("[traceback]", text)
    text = " ".join(text.split())
    return text or type(exc).__name__


def _new_nonce() -> str:
    return os.urandom(8).hex()


# --- source gathering -----------------------------------------------------

def _path_context(logical: str) -> str:
    """Top-level directory of a logical source path ('sop' for 'sop/x.txt')."""
    head, _sep, _tail = logical.partition("/")
    return head


def _dispatch_archive_page(
    archive_name: str, logical: str, text: str
) -> ParsedDocument | None:
    """Dispatch one archive page to its parser, respecting archive context.

    ``nodes.zip`` yields node documents only for top-level ``sop/*.txt`` pages
    with ``#type: node``; pages in ``vop/``, ``obj/``, ``dop/``, ``apex/``,
    ``shop/``, ``sop_state/`` etc. are never parsed as SOP nodes. ``hom.zip``
    and ``vex.zip`` keep their existing ``#type`` dispatch. Unknown or
    unsupported types produce no entity.
    """
    doc_type = parse_metadata(text).get("type", "").strip()
    if archive_name == "nodes.zip":
        if _path_context(logical) != "sop" or doc_type != "node":
            return None
        return parse_node_document(logical, text)
    if archive_name == "hom.zip":
        if doc_type in _HOM_TYPES:
            return parse_hom_document(logical, text)
        return None
    if archive_name == "vex.zip":
        if doc_type == "vex":
            return parse_vex_document(logical, text)
        return None
    return None


def parse_archive_documents(
    archive_name: str, entries: list[tuple[str, bytes]]
) -> list[ParsedDocument]:
    """Parse archive entries with archive-aware dispatch.

    Archive boundaries are preserved: the ``archive_name`` decides which parser
    and path rules apply, so a flattened entry list cannot turn non-SOP node
    pages into ``node_document`` entities.
    """
    docs: list[ParsedDocument] = []
    for logical, data in entries:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        document = _dispatch_archive_page(archive_name, logical, text)
        if document is not None:
            docs.append(document)
    return docs


def _load_three_archives(hfs: Path):
    """Read the three required archives, keeping per-archive entry boundaries."""
    fingerprints = []
    per_archive: dict[str, list[tuple[str, bytes]]] = {}
    for name in _REQUIRED_ARCHIVES:
        data = (hfs / "houdini" / "help" / name).read_bytes()
        fingerprints.append(fingerprint_bytes(name, data))
        per_archive[name] = load_archive_entries(data)
    return tuple(fingerprints), per_archive


def _gather_hfs_sources(
    options: BuildOptions, *, runner, repo_root: Path
):
    hfs = resolve_hfs(
        str(options.hfs) if options.hfs else None,
        dict(os.environ),
        _DEFAULT_HFS_CANDIDATES,
    )
    snapshot = load_inventory_snapshot(hfs, runner=runner)
    archive_fps, per_archive = _load_three_archives(hfs)
    skill_fps, skill_entries = load_skill_sources(repo_root / "skills", repo_root)
    docs: list[ParsedDocument] = []
    for name in _REQUIRED_ARCHIVES:
        docs.extend(parse_archive_documents(name, per_archive[name]))
    for logical, data in skill_entries:
        docs.append(parse_skill_document(logical, data.decode("utf-8-sig")))
    return docs, snapshot, archive_fps, skill_fps


# --- manifest -------------------------------------------------------------

def _count_ambiguous_aliases(bundle) -> int:
    grouped: dict[str, list] = defaultdict(list)
    for alias in bundle.aliases:
        grouped[alias.alias].append(alias)
    ambiguous = 0
    for group in grouped.values():
        top = max(a.priority for a in group)
        owners = {a.entity_id for a in group if a.priority == top}
        if len(owners) > 1:
            ambiguous += 1
    return ambiguous


def _build_manifest(
    bundle, snapshot: HythonInventorySnapshot, archive_fps, skill_fps, created_at_utc: str
) -> BuildManifest:
    entity_counts = Counter(e.kind.value for e in bundle.entities)
    edge_counts = Counter(e.predicate for e in bundle.edges)
    unresolved = sum(1 for e in bundle.edges if not e.resolved)
    ambiguous = _count_ambiguous_aliases(bundle)
    return build_manifest(
        kb_schema_version=KB_SCHEMA_VERSION,
        builder_version=BUILDER_VERSION,
        parser_version=PARSER_VERSION,
        created_at_utc=created_at_utc,
        houdini_version=snapshot.houdini_version,
        houdini_build=snapshot.houdini_build,
        source_archives=archive_fps,
        skill_sources=skill_fps,
        node_inventory_sha256=hash_inventory(snapshot.sop_node_types),
        entity_count_by_kind=entity_counts,
        edge_count_by_predicate=edge_counts,
        unresolved_reference_count=unresolved,
        ambiguous_alias_count=ambiguous,
    )


# --- selftest corpus ------------------------------------------------------

_SELFTEST_NODE = (
    "#type: node\n#context: sop\n#tags: model\n#internal: boolean\n"
    "= Boolean =\n\"\"\"Boolean op.\"\"\"\n"
    "== Overview == (overview)\nSee [Vex:intersect].\n"
)
_SELFTEST_VEX = (
    "#type: vex\n#context: sop\n#group: geometry\n= intersect =\n\"\"\"Intersect.\"\"\"\n"
    ":usage: intersect(geo) -> int\n"
)
_SELFTEST_HOM = (
    "= hou.Node =\n#type: homclass\n#superclass: hou.NodeReferenceCounted\n"
    "\"\"\"Node class.\"\"\"\n"
    "::`createNode(self, type_name)` -> [Hom:hou.Node]:\n    Creates a node.\n"
)
_SELFTEST_SKILL = (
    "---\nname: vex-patterns\ndescription: reusable VEX snippets.\n---\n\n"
    "# VEX Patterns\n\n## Overview\nUse [Vex:intersect].\n"
)


def _selftest_corpus():
    docs = [
        parse_node_document("sop/boolean.txt", _SELFTEST_NODE),
        parse_vex_document("functions/intersect.txt", _SELFTEST_VEX),
        parse_hom_document("hou/Node.txt", _SELFTEST_HOM),
        parse_skill_document("skills/vex-patterns/SKILL.md", _SELFTEST_SKILL),
    ]
    snapshot = HythonInventorySnapshot(
        "21.0.440", "21.0.440", frozenset({"boolean"})
    )
    archive_fps = (
        fingerprint_bytes("nodes.zip", b"<nodes>"),
        fingerprint_bytes("hom.zip", b"<hom>"),
        fingerprint_bytes("vex.zip", b"<vex>"),
    )
    skill_fps = (
        fingerprint_bytes("skills/vex-patterns/SKILL.md", _SELFTEST_SKILL.encode("utf-8")),
    )
    return docs, snapshot, archive_fps, skill_fps


# --- build orchestration --------------------------------------------------

def _cleanup_temp(tmp_path: Path) -> None:
    """Remove this build's temp database and any sidecar files (this nonce only)."""
    for path in tmp_path.parent.glob(tmp_path.name + "*"):
        try:
            path.unlink()
        except OSError:
            pass


def build_cache(
    options: BuildOptions,
    *,
    now: Callable[[], datetime] = _production_now,
    pid_exists: Callable[[int], bool] = _production_pid_exists,
    runner=subprocess.run,
    writer=write_cache,
    validator=validate_cache,
    repo_root: Path | None = None,
) -> BuildManifest:
    """Build the cache at ``options.output`` and return its manifest.

    External boundaries (``now``, ``pid_exists``, ``runner``, ``writer``,
    ``validator``, ``repo_root``) are injectable so tests need no HFS, hython or
    real clock. On any failure the previous cache is preserved (the final path
    is touched only by the final ``os.replace``) and this build's temp file is
    removed; the lock is always released.
    """
    output = Path(options.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = lock_path_for(output)
    pid = os.getpid()
    started_at = now()
    nonce = _new_nonce()

    try:
        acquire_build_lock(
            lock_path,
            pid=pid,
            started_at=started_at,
            nonce=nonce,
            pid_exists=pid_exists,
            now=now,
            stale_ttl_seconds=LOCK_STALE_TTL_SECONDS,
        )
    except LockError as exc:
        raise BuildError("build lock busy or unavailable") from exc

    tmp_path = output.with_name(f"{output.name}.tmp.{pid}.{nonce}")
    try:
        if options.selftest:
            docs, snapshot, archive_fps, skill_fps = _selftest_corpus()
        else:
            root = repo_root if repo_root is not None else _default_repo_root()
            docs, snapshot, archive_fps, skill_fps = _gather_hfs_sources(
                options, runner=runner, repo_root=root
            )
        bundle = assemble_graph(tuple(docs), inventory=snapshot.sop_node_types)
        validate_graph(bundle)
        manifest = _build_manifest(
            bundle, snapshot, archive_fps, skill_fps, started_at.isoformat()
        )
        writer(tmp_path, bundle, manifest)
        validator(tmp_path)
        os.replace(tmp_path, output)
        return manifest
    except BuildError:
        _cleanup_temp(tmp_path)
        raise
    except Exception as exc:  # source/graph/write/validation failure
        _cleanup_temp(tmp_path)
        raise BuildError(_sanitize_error(exc)) from exc
    finally:
        release_build_lock(lock_path, nonce=nonce)


# --- CLI ------------------------------------------------------------------

def _summary(manifest: BuildManifest, *, selftest: bool) -> dict:
    return {
        "ok": True,
        "selftest": selftest,
        "validated": True,
        "kb_schema_version": manifest.kb_schema_version,
        "builder_version": manifest.builder_version,
        "parser_version": manifest.parser_version,
        "houdini_version": manifest.houdini_version,
        "houdini_build": manifest.houdini_build,
        "manifest_sha256": manifest.manifest_sha256,
        "node_inventory_sha256": manifest.node_inventory_sha256,
        "entity_count": sum(c for _, c in manifest.entity_count_by_kind),
        "edge_count": sum(c for _, c in manifest.edge_count_by_predicate),
        "entity_count_by_kind": dict(manifest.entity_count_by_kind),
        "edge_count_by_predicate": dict(manifest.edge_count_by_predicate),
        "unresolved_reference_count": manifest.unresolved_reference_count,
        "ambiguous_alias_count": manifest.ambiguous_alias_count,
    }


def _run_selftest() -> int:
    with tempfile.TemporaryDirectory(prefix="eee-kb-selftest-") as tmp_dir:
        output = Path(tmp_dir) / "knowledge.sqlite3"
        try:
            manifest = build_cache(BuildOptions(output=output, selftest=True))
        except BuildError as exc:
            print(json.dumps({"ok": False, "error": _sanitize_error(exc)}), file=sys.stderr)
            return 1
        summary = _summary(manifest, selftest=True)
        summary["temp_cleaned"] = True
        print(json.dumps(summary))
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Build the cache. ``--selftest`` needs no HFS and leaves no cache behind."""
    parser = argparse.ArgumentParser(prog="eee_agent.knowledge.build")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--hfs", default=None)
    parser.add_argument("--out", default=None)
    namespace = parser.parse_args(argv)

    if namespace.selftest:
        return _run_selftest()
    if not namespace.out:
        print(
            json.dumps({"ok": False, "error": "--out is required for a build"}),
            file=sys.stderr,
        )
        return 2
    options = BuildOptions(
        output=Path(namespace.out),
        hfs=Path(namespace.hfs) if namespace.hfs else None,
    )
    try:
        manifest = build_cache(options)
    except BuildError as exc:
        print(json.dumps({"ok": False, "error": _sanitize_error(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(_summary(manifest, selftest=False)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
