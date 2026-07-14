"""Deterministic source fingerprints and build manifest.

Pure and I/O-free: operates only on supplied bytes/values. The canonical
payload excludes ``created_at_utc``, ``manifest_sha256``, the payload itself
and all absolute machine paths; the same logical inputs in any collection
order produce an equal payload and hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping

__all__ = [
    "BuildManifest",
    "SourceFingerprint",
    "build_manifest",
    "fingerprint_bytes",
    "hash_inventory",
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _validate_logical_name(name: str) -> None:
    if not name or not name.strip():
        raise ValueError("logical_name must be a non-empty logical path")
    if any(ord(ch) <= 0x1F or ord(ch) == 0x7F for ch in name):
        raise ValueError("logical_name contains a control character")
    if _DRIVE_RE.match(name):
        raise ValueError("logical_name must not have a Windows drive prefix")
    if name.startswith("/"):
        raise ValueError("logical_name must not be an absolute POSIX path")
    for segment in name.split("/"):
        if segment in ("", ".", ".."):
            raise ValueError("logical_name contains a traversal or empty segment")


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    """A fingerprint for a Houdini source archive or a project Skill source."""

    logical_name: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_logical_name(self.logical_name)
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        if not _SHA256_RE.match(self.sha256):
            raise ValueError("sha256 must be exactly 64 lowercase hexadecimal characters")


def _normalize_counts(counts) -> tuple[tuple[str, int], ...]:
    if isinstance(counts, Mapping):
        items = counts.items()
    else:
        items = counts
    return tuple(sorted((str(k), int(v)) for k, v in items))


def _archive_dict(sf: SourceFingerprint) -> dict:
    return {"logical_name": sf.logical_name, "size_bytes": sf.size_bytes, "sha256": sf.sha256}


def _skill_dict(sf: SourceFingerprint) -> dict:
    return {"path": sf.logical_name, "sha256": sf.sha256}


@dataclass(frozen=True, slots=True)
class BuildManifest:
    """A deterministic build manifest with its computed canonical payload and hash."""

    kb_schema_version: int
    builder_version: str
    parser_version: str
    created_at_utc: str
    houdini_version: str
    houdini_build: str
    manifest_sha256: str
    source_archives: tuple[SourceFingerprint, ...]
    skill_sources: tuple[SourceFingerprint, ...]
    node_inventory_sha256: str
    entity_count_by_kind: tuple[tuple[str, int], ...]
    edge_count_by_predicate: tuple[tuple[str, int], ...]
    unresolved_reference_count: int
    ambiguous_alias_count: int
    canonical_payload: str

    def to_metadata(self) -> dict:
        """Return the full serializable metadata (includes time, hash, payload)."""
        return {
            "kb_schema_version": self.kb_schema_version,
            "builder_version": self.builder_version,
            "parser_version": self.parser_version,
            "created_at_utc": self.created_at_utc,
            "houdini_version": self.houdini_version,
            "houdini_build": self.houdini_build,
            "manifest_sha256": self.manifest_sha256,
            "source_archives": [_archive_dict(sf) for sf in self.source_archives],
            "skill_sources": [_skill_dict(sf) for sf in self.skill_sources],
            "node_inventory_sha256": self.node_inventory_sha256,
            "entity_count_by_kind": dict(self.entity_count_by_kind),
            "edge_count_by_predicate": dict(self.edge_count_by_predicate),
            "unresolved_reference_count": self.unresolved_reference_count,
            "ambiguous_alias_count": self.ambiguous_alias_count,
            "canonical_payload": self.canonical_payload,
        }


def build_manifest(
    *,
    kb_schema_version: int,
    builder_version: str,
    parser_version: str,
    created_at_utc: str,
    houdini_version: str,
    houdini_build: str,
    source_archives,
    skill_sources,
    node_inventory_sha256: str,
    entity_count_by_kind,
    edge_count_by_predicate,
    unresolved_reference_count: int,
    ambiguous_alias_count: int,
) -> BuildManifest:
    """Build a deterministic :class:`BuildManifest` from explicit keyword inputs.

    Archives, skills and count mappings are normalized to sorted immutable
    tuples. The canonical payload excludes ``created_at_utc``,
    ``manifest_sha256`` and itself, so changing only the time leaves the hash
    unchanged; changing any deterministic input changes the hash.
    """
    sorted_archives = tuple(sorted(source_archives, key=lambda sf: sf.logical_name))
    sorted_skills = tuple(sorted(skill_sources, key=lambda sf: sf.logical_name))
    entity_counts = _normalize_counts(entity_count_by_kind)
    edge_counts = _normalize_counts(edge_count_by_predicate)

    payload = {
        "kb_schema_version": kb_schema_version,
        "builder_version": builder_version,
        "parser_version": parser_version,
        "houdini_version": houdini_version,
        "houdini_build": houdini_build,
        "source_archives": [_archive_dict(sf) for sf in sorted_archives],
        "skill_sources": [_skill_dict(sf) for sf in sorted_skills],
        "node_inventory_sha256": node_inventory_sha256,
        "entity_count_by_kind": dict(entity_counts),
        "edge_count_by_predicate": dict(edge_counts),
        "unresolved_reference_count": unresolved_reference_count,
        "ambiguous_alias_count": ambiguous_alias_count,
    }
    canonical_payload = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    manifest_sha256 = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()

    return BuildManifest(
        kb_schema_version=kb_schema_version,
        builder_version=builder_version,
        parser_version=parser_version,
        created_at_utc=created_at_utc,
        houdini_version=houdini_version,
        houdini_build=houdini_build,
        manifest_sha256=manifest_sha256,
        source_archives=sorted_archives,
        skill_sources=sorted_skills,
        node_inventory_sha256=node_inventory_sha256,
        entity_count_by_kind=entity_counts,
        edge_count_by_predicate=edge_counts,
        unresolved_reference_count=unresolved_reference_count,
        ambiguous_alias_count=ambiguous_alias_count,
        canonical_payload=canonical_payload,
    )


def fingerprint_bytes(logical_name: str, data: bytes) -> SourceFingerprint:
    """Fingerprint supplied bytes (no file access)."""
    return SourceFingerprint(
        logical_name=logical_name,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def hash_inventory(inventory: frozenset[str]) -> str:
    """Hash a SOP inventory by sorting exact case-sensitive names canonically."""
    names = sorted(inventory)
    canonical = json.dumps(names, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
