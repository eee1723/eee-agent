"""Stage 7 Task 14 — opt-in real Houdini 21.0.440 corpus contract.

These tests are skipped by default and run only when
``EEE_RUN_HOUDINI_KB_TESTS=true``. When enabled they build the cache from the
local HFS into a pytest tmp dir (never the repo), then assert the audited
corpus invariants: entity counts, verified operators, inventory membership,
resolved-edge integrity, manifest counts, exact SOP source scope, the four
project skills, and the absence of any stored absolute machine path. No RPC and
no GUI are used.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterator

import pytest

pytestmark = [
    pytest.mark.houdini_kb,
    pytest.mark.skipif(
        os.getenv("EEE_RUN_HOUDINI_KB_TESTS", "").strip().lower() != "true",
        reason="set EEE_RUN_HOUDINI_KB_TESTS=true (needs local Houdini 21.0.440 HFS + hython)",
    ),
]

# Verified two-machine HFS roots (design §9.1), used only to locate sources.
_HFS_CANDIDATES = (
    Path("D:/houdini"),
    Path("C:/Program Files/Side Effects Software/Houdini 21.0.440"),
)
# A stored Windows drive path (single drive letter, not a URL scheme like
# ``https://``) or a UNC root. URL schemes are excluded via the lookbehind so
# doc links are not mistaken for machine paths.
_MACHINE_PATH_RE = re.compile(r"(?:\\\\|(?<![A-Za-z0-9])[A-Za-z]:[\\/])")

_REQUIRED_SKILL_PATHS = {
    "skills/html-to-houdini/SKILL.md",
    "skills/parametric-building/SKILL.md",
    "skills/procedural-components/SKILL.md",
    "skills/procedural-modeling/SKILL.md",
    "skills/sop-cookbook/SKILL.md",
    "skills/vex-patterns/SKILL.md",
}


def _store(path: Path):
    from eee_agent.knowledge.store import KnowledgeStore

    return KnowledgeStore.open(path)


def _metadata(path: Path) -> dict:
    with _store(path) as store:
        return store.metadata()


def _verified_operators(path: Path) -> dict[str, int]:
    """operator_type -> count of current docs verified against the inventory."""
    out: dict[str, int] = {}
    with _store(path) as store:
        rows = store.connection.execute(
            "SELECT attributes_json FROM entities WHERE kind='node_document'"
        ).fetchall()
    for row in rows:
        attrs = json.loads(row[0])
        if attrs.get("operator_type_status") == "verified_at_build":
            op = attrs.get("operator_type")
            if isinstance(op, str):
                out[op] = out.get(op, 0) + 1
    return out


@pytest.fixture(scope="module")
def kb_build(tmp_path_factory):
    """Build the real cache once into a pytest tmp dir; return build context."""
    from eee_agent.knowledge.build import BuildOptions, build_cache
    from eee_agent.knowledge.inventory import load_inventory_snapshot, resolve_hfs

    hfs = resolve_hfs(None, dict(os.environ), _HFS_CANDIDATES)
    cache_path = tmp_path_factory.mktemp("hfs-kb") / "knowledge.sqlite3"
    build_cache(BuildOptions(output=cache_path, hfs=hfs))
    snapshot = load_inventory_snapshot(hfs)
    return {
        "path": cache_path,
        "hfs": hfs,
        "inventory": snapshot.sop_node_types,
        "houdini_build": snapshot.houdini_build,
    }


def test_houdini_build_is_21_0_440(kb_build) -> None:
    meta = _metadata(kb_build["path"])
    assert meta["houdini_version"] == "21.0.440"
    assert meta["houdini_build"] == "21.0.440"
    assert kb_build["houdini_build"] == "21.0.440"


def test_corpus_entity_counts_in_audited_ranges(kb_build) -> None:
    counts = _metadata(kb_build["path"])["entity_count_by_kind"]
    assert 1100 <= counts["node_document"] <= 1130
    assert 1080 <= counts["vex_function"] <= 1120
    assert 330 <= counts["hom_class"] <= 350
    assert 5300 <= counts["hom_method"] <= 5650
    # Sanity: the other audited kinds are present and non-empty.
    assert counts["hom_function"] > 0
    assert counts["hom_module"] > 0
    assert counts["skill_reference"] == len(_REQUIRED_SKILL_PATHS)


def test_required_operators_verified_at_build(kb_build) -> None:
    verified = _verified_operators(kb_build["path"])
    for operator in ("apex::buildfkgraph", "loadslices", "kinefx::rigpython"):
        assert verified.get(operator, 0) >= 1, (
            f"{operator!r} is not verified_at_build"
        )


def test_every_verified_operator_exists_in_inventory(kb_build) -> None:
    inventory = kb_build["inventory"]
    verified = _verified_operators(kb_build["path"])
    assert verified, "expected at least one verified operator"
    for operator in verified:
        assert operator in inventory, (
            f"verified operator {operator!r} is not in the hython SOP inventory"
        )


def test_resolved_edge_targets_exist(kb_build) -> None:
    with _store(kb_build["path"]) as store:
        dangling = store.connection.execute(
            "SELECT COUNT(*) FROM edges WHERE resolved=1 AND target_id IS NOT NULL "
            "AND target_id NOT IN (SELECT entity_id FROM entities)"
        ).fetchone()[0]
    assert dangling == 0


def test_manifest_records_unresolved_and_ambiguous_counts(kb_build) -> None:
    meta = _metadata(kb_build["path"])
    unresolved = meta["unresolved_reference_count"]
    ambiguous = meta["ambiguous_alias_count"]
    assert isinstance(unresolved, int) and not isinstance(unresolved, bool)
    assert isinstance(ambiguous, int) and not isinstance(ambiguous, bool)
    # The real corpus has genuinely unresolved refs and ambiguous aliases.
    assert unresolved > 0
    assert ambiguous > 0


def test_manifest_hash_is_sha256(kb_build) -> None:
    sha = _metadata(kb_build["path"])["manifest_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", sha)


def _iter_strings(obj) -> Iterator[str]:
    """Yield every string in a decoded JSON structure (dict/list/scalar)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_strings(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _iter_strings(value)


def test_no_absolute_machine_path_stored(kb_build) -> None:
    path = kb_build["path"]
    texts: list[str] = []
    with _store(path) as store:
        for row in store.connection.execute(
            "SELECT source_path, source_anchor, canonical_name, title, summary, "
            "attributes_json FROM entities"
        ).fetchall():
            # Plain text columns (not JSON-escaped).
            texts.extend(str(row[i] or "") for i in range(5))
            # Attributes are scanned as decoded values so JSON escape sequences
            # (e.g. ``\\n``) are not mistaken for machine paths.
            texts.extend(_iter_strings(json.loads(row[5])))
        for row in store.connection.execute(
            "SELECT target_raw, target_anchor, source_location FROM edges"
        ).fetchall():
            texts.extend(str(row[i] or "") for i in range(3))
    texts.extend(_iter_strings(_metadata(path)))
    leaked = [t for t in texts if _MACHINE_PATH_RE.search(t)]
    assert not leaked, f"absolute machine path leaked into cache: {leaked[:3]!r}"


def test_sop_source_scope_is_exact(kb_build) -> None:
    with _store(kb_build["path"]) as store:
        rows = store.connection.execute(
            "SELECT source_path FROM entities WHERE kind='node_document'"
        ).fetchall()
    assert rows, "expected node documents"
    for row in rows:
        source = row[0]
        parts = source.split("/")
        # Only top-level sop/<name>.txt pages become node documents (archive
        # dispatch boundary); nested or non-txt sop entries never do.
        assert len(parts) == 2 and parts[0] == "sop" and parts[1].endswith(".txt"), (
            f"unexpected node_document source path: {source!r}"
        )


def test_six_project_skills_represented(kb_build) -> None:
    with _store(kb_build["path"]) as store:
        rows = store.connection.execute(
            "SELECT source_path, authority FROM entities WHERE kind='skill_reference'"
        ).fetchall()
    assert len(rows) == 6
    assert {row[0] for row in rows} == _REQUIRED_SKILL_PATHS
    assert all(row[1] == "project_verified_skill" for row in rows)


def test_cache_passes_writer_self_checks(kb_build) -> None:
    from eee_agent.knowledge.writer import validate_cache

    validate_cache(kb_build["path"])  # raises CacheIntegrityError on failure
