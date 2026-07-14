"""Tests for deterministic build fingerprints and the build manifest.

Pure, I/O-free: synthetic hashes and bytes only. No real archives or skills.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from eee_agent.knowledge.manifest import (
    BuildManifest,
    SourceFingerprint,
    build_manifest,
    fingerprint_bytes,
    hash_inventory,
)


def _sf(logical_name: str = "nodes.zip", size: int = 100, sha: str = "a" * 64) -> SourceFingerprint:
    return SourceFingerprint(logical_name=logical_name, size_bytes=size, sha256=sha)


def _build(**overrides) -> BuildManifest:
    defaults: dict = dict(
        kb_schema_version=1,
        builder_version="b1",
        parser_version="p1",
        created_at_utc="2026-07-14T01:00:00Z",
        houdini_version="21.0.440",
        houdini_build="21.0.440",
        source_archives=(_sf(),),
        skill_sources=(),
        node_inventory_sha256="c" * 64,
        entity_count_by_kind={"node_document": 10},
        edge_count_by_predicate={"references": 5},
        unresolved_reference_count=1,
        ambiguous_alias_count=0,
    )
    defaults.update(overrides)
    return build_manifest(**defaults)


# --- SourceFingerprint ----------------------------------------------------

def test_source_fingerprint_frozen_and_slots() -> None:
    sf = _sf()
    assert dataclasses.is_dataclass(sf)
    with pytest.raises(dataclasses.FrozenInstanceError):
        sf.logical_name = "x"  # type: ignore[misc]
    assert not hasattr(sf, "__dict__")


@pytest.mark.parametrize(
    "name",
    ["C:/houdini/nodes.zip", "/etc/nodes.zip", "../nodes.zip", "", "a\x00b", "a//b"],
)
def test_invalid_logical_name_rejected(name: str) -> None:
    with pytest.raises(ValueError):
        SourceFingerprint(logical_name=name, size_bytes=10, sha256="a" * 64)


@pytest.mark.parametrize("sha", ["abc", "A" * 64, "g" * 64, ""])
def test_invalid_sha256_rejected(sha: str) -> None:
    with pytest.raises(ValueError):
        SourceFingerprint(logical_name="nodes.zip", size_bytes=10, sha256=sha)


def test_negative_size_rejected() -> None:
    with pytest.raises(ValueError):
        SourceFingerprint(logical_name="nodes.zip", size_bytes=-1, sha256="a" * 64)


def test_fingerprint_bytes() -> None:
    sf = fingerprint_bytes("nodes.zip", b"hello")
    assert sf.size_bytes == 5
    assert len(sf.sha256) == 64
    import hashlib
    assert sf.sha256 == hashlib.sha256(b"hello").hexdigest()


# --- canonical payload and hashing ---------------------------------------

def test_canonical_json_exact_separators_and_sorted_keys() -> None:
    manifest = _build()
    payload = manifest.canonical_payload
    assert ", " not in payload
    assert ": " not in payload
    assert payload == json.dumps(
        json.loads(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def test_time_excluded_from_hash() -> None:
    m1 = _build(created_at_utc="2026-07-14T01:00:00Z")
    m2 = _build(created_at_utc="2026-07-14T02:00:00Z")
    assert m1.manifest_sha256 == m2.manifest_sha256
    assert m1.canonical_payload == m2.canonical_payload
    assert "2026-07-14" not in m1.canonical_payload


def test_no_absolute_path_in_canonical_payload() -> None:
    manifest = _build(source_archives=(_sf("nodes.zip"), _sf("hom.zip")))
    payload = manifest.canonical_payload
    assert "E:\\" not in payload
    assert "C:\\" not in payload
    assert "/home/" not in payload
    assert "/Users/" not in payload


def test_archive_input_order_independence() -> None:
    a, b = _sf("nodes.zip"), _sf("hom.zip")
    m1 = _build(source_archives=(a, b))
    m2 = _build(source_archives=(b, a))
    assert m1.manifest_sha256 == m2.manifest_sha256


def test_skill_input_order_independence() -> None:
    a, b = _sf("skills/vex-patterns/SKILL.md", size=0), _sf("skills/sop-cookbook/SKILL.md", size=0)
    m1 = _build(skill_sources=(a, b))
    m2 = _build(skill_sources=(b, a))
    assert m1.manifest_sha256 == m2.manifest_sha256


def test_count_mapping_order_independence() -> None:
    m1 = _build(entity_count_by_kind={"node_document": 10, "vex_function": 20})
    m2 = _build(entity_count_by_kind={"vex_function": 20, "node_document": 10})
    assert m1.manifest_sha256 == m2.manifest_sha256
    m3 = _build(entity_count_by_kind=(("node_document", 10), ("vex_function", 20)))
    assert m1.manifest_sha256 == m3.manifest_sha256


def test_archive_hash_change_changes_manifest() -> None:
    m1 = _build(source_archives=(_sf(sha="a" * 64),))
    m2 = _build(source_archives=(_sf(sha="b" * 64),))
    assert m1.manifest_sha256 != m2.manifest_sha256


def test_skill_hash_change_changes_manifest() -> None:
    m1 = _build(skill_sources=(_sf("skills/x/SKILL.md", size=0, sha="a" * 64),))
    m2 = _build(skill_sources=(_sf("skills/x/SKILL.md", size=0, sha="b" * 64),))
    assert m1.manifest_sha256 != m2.manifest_sha256


def test_inventory_hash_change_changes_manifest() -> None:
    m1 = _build(node_inventory_sha256="c" * 64)
    m2 = _build(node_inventory_sha256="d" * 64)
    assert m1.manifest_sha256 != m2.manifest_sha256


def test_counts_change_manifest() -> None:
    m1 = _build(entity_count_by_kind={"node_document": 10})
    m2 = _build(entity_count_by_kind={"node_document": 11})
    assert m1.manifest_sha256 != m2.manifest_sha256


def test_version_build_changes_manifest() -> None:
    m1 = _build(houdini_version="21.0.440")
    m2 = _build(houdini_version="21.0.500")
    assert m1.manifest_sha256 != m2.manifest_sha256
    m3 = _build(builder_version="b1")
    m4 = _build(builder_version="b2")
    assert m3.manifest_sha256 != m4.manifest_sha256


def test_created_at_utc_retained() -> None:
    manifest = _build(created_at_utc="2026-07-14T05:00:00Z")
    assert manifest.created_at_utc == "2026-07-14T05:00:00Z"


def test_manifest_sha256_retained() -> None:
    manifest = _build()
    assert len(manifest.manifest_sha256) == 64
    assert all(c in "0123456789abcdef" for c in manifest.manifest_sha256)


def test_repeated_builds_identical_hashes() -> None:
    m1 = _build()
    m2 = _build()
    assert m1.manifest_sha256 == m2.manifest_sha256
    assert m1.canonical_payload == m2.canonical_payload


def test_build_manifest_frozen_and_slots() -> None:
    manifest = _build()
    assert dataclasses.is_dataclass(manifest)
    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.kb_schema_version = 2  # type: ignore[misc]
    assert not hasattr(manifest, "__dict__")


def test_metadata_includes_created_at_and_sha() -> None:
    manifest = _build(created_at_utc="2026-07-14T05:00:00Z")
    metadata = manifest.to_metadata()
    assert metadata["created_at_utc"] == "2026-07-14T05:00:00Z"
    assert metadata["manifest_sha256"] == manifest.manifest_sha256
    assert metadata["canonical_payload"] == manifest.canonical_payload
    # archives serialize with logical_name/size_bytes/sha256
    assert metadata["source_archives"][0] == {
        "logical_name": "nodes.zip", "size_bytes": 100, "sha256": "a" * 64,
    }
    # skills serialize with path/sha256 only
    manifest2 = _build(skill_sources=(_sf("skills/x/SKILL.md", size=0, sha="e" * 64),))
    assert manifest2.to_metadata()["skill_sources"][0] == {
        "path": "skills/x/SKILL.md", "sha256": "e" * 64,
    }


def test_hash_inventory_deterministic_and_case_sensitive() -> None:
    inv1 = frozenset({"apex::buildfkgraph", "loadslices"})
    inv2 = frozenset({"loadslices", "apex::buildfkgraph"})
    assert hash_inventory(inv1) == hash_inventory(inv2)
    assert hash_inventory(frozenset({"Apex::buildfkgraph"})) != hash_inventory(frozenset({"apex::buildfkgraph"}))
