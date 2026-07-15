"""Tests for safe source loading, the stale-aware build lock and the atomic
explicit builder.

No real Houdini installation is required: HFS is never read (the ``--selftest``
synthetic corpus and injected locks/runners drive every path).
"""

from __future__ import annotations

import io
import json
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from eee_agent.knowledge.build import (
    BuildError,
    BuildOptions,
    build_cache,
    main,
    parse_archive_documents,
)
from eee_agent.knowledge.graph import assemble_graph
from eee_agent.knowledge.lock import (
    LOCK_STALE_TTL_SECONDS,
    LockBusy,
    acquire_build_lock,
    lock_path_for,
    release_build_lock,
)
from eee_agent.knowledge.models import EntityKind
from eee_agent.knowledge.sources import SourceError, load_archive_entries, load_skill_sources
from eee_agent.knowledge.writer import validate_cache


def _dt(seconds: int) -> datetime:
    return datetime(2026, 7, 15, 12, 0, 0) + timedelta(seconds=seconds)


def _make_zip(entries) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


REQUIRED_SKILL_NAMES = (
    "parametric-building",
    "procedural-components",
    "sop-cookbook",
    "vex-patterns",
)


def _make_skills(tmp_path: Path, names=REQUIRED_SKILL_NAMES) -> Path:
    skills = tmp_path / "skills"
    for name in names:
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n\n# {name}\nbody\n")
    return skills


# --- sources --------------------------------------------------------------

def test_load_archive_entries_returns_logical_names() -> None:
    data = _make_zip([("sop/boolean.txt", b"x"), ("hou/Node.txt", b"y")])
    entries = load_archive_entries(data)
    assert {logical for logical, _ in entries} == {"sop/boolean.txt", "hou/Node.txt"}
    assert dict(entries)["sop/boolean.txt"] == b"x"


def test_load_archive_entries_normalizes_backslash_names() -> None:
    data = _make_zip([("sop\\boolean.txt", b"x")])
    assert {logical for logical, _ in load_archive_entries(data)} == {"sop/boolean.txt"}


@pytest.mark.parametrize("bad", ["../escape.txt", "/abs.txt", "C:/drive.txt", "a/../../b.txt"])
def test_load_archive_entries_rejects_unsafe(bad: str) -> None:
    with pytest.raises(SourceError):
        load_archive_entries(_make_zip([(bad, b"x")]))


def test_load_skill_sources_requires_exactly_four_skills(tmp_path: Path) -> None:
    skills = _make_skills(tmp_path)
    fps, entries = load_skill_sources(skills, tmp_path)
    logicals = {fp.logical_name for fp in fps}
    assert logicals == {
        "skills/parametric-building/SKILL.md",
        "skills/procedural-components/SKILL.md",
        "skills/sop-cookbook/SKILL.md",
        "skills/vex-patterns/SKILL.md",
    }
    assert {logical for logical, _ in entries} == logicals
    assert len(fps) == 4


def test_load_skill_sources_missing_skill_fails(tmp_path: Path) -> None:
    skills = _make_skills(tmp_path, names=REQUIRED_SKILL_NAMES[:3])  # omit one
    with pytest.raises(SourceError):
        load_skill_sources(skills, tmp_path)


def test_load_skill_sources_extra_skill_fails(tmp_path: Path) -> None:
    skills = _make_skills(tmp_path)
    (skills / "extra").mkdir()
    (skills / "extra" / "SKILL.md").write_text("# extra\n")
    with pytest.raises(SourceError):
        load_skill_sources(skills, tmp_path)


# --- node source dispatch (archive context restriction) -------------------

_NODE_TXT = (
    b"#type: node\n#context: sop\n#internal: boolean\n"
    b"= Boolean =\n\"\"\"Boolean op.\"\"\"\n"
)
_NON_SOP_NODE_TXT = b"#type: node\n= X =\n\"\"\"non-sop node page.\"\"\"\n"
_HOM_TXT = (
    b"= hou.Node =\n#type: homclass\n\"\"\"Node class.\"\"\"\n"
)
_VEX_TXT = (
    b"#type: vex\n#context: sop\n= intersect =\n\"\"\"Intersect.\"\"\"\n"
    b":usage: intersect(geo) -> int\n"
)


def test_nodes_archive_only_parses_sop_context_node_pages() -> None:
    entries = [
        ("sop/boolean.txt", _NODE_TXT),
        ("obj/not_sop.txt", _NON_SOP_NODE_TXT),
        ("vop/not_sop.txt", _NON_SOP_NODE_TXT),
    ]
    docs = parse_archive_documents("nodes.zip", entries)
    entities = [e for d in docs for e in d.entities]
    assert {e.entity_id for e in entities} == {
        "node_document:sop/boolean.txt@current"
    }


@pytest.mark.parametrize("ctx", ["vop", "obj", "dop", "apex", "shop", "sop_state"])
def test_nodes_archive_excludes_non_sop_contexts(ctx: str) -> None:
    docs = parse_archive_documents("nodes.zip", [(f"{ctx}/x.txt", _NON_SOP_NODE_TXT)])
    assert docs == []


def test_nodes_archive_excludes_non_node_type_under_sop() -> None:
    docs = parse_archive_documents(
        "nodes.zip", [("sop/_common.txt", b"#type: include\n= c =\n\"\"\"c.\"\"\"\n")]
    )
    assert docs == []


def test_node_dispatch_excludes_non_sop_from_graph_counts() -> None:
    entries = [
        ("sop/boolean.txt", _NODE_TXT),
        ("vop/not_sop.txt", _NON_SOP_NODE_TXT),
        ("obj/not_sop.txt", _NON_SOP_NODE_TXT),
        ("dop/not_sop.txt", _NON_SOP_NODE_TXT),
    ]
    docs = parse_archive_documents("nodes.zip", entries)
    bundle = assemble_graph(tuple(docs), inventory=frozenset({"boolean"}))
    counts = Counter(e.kind.value for e in bundle.entities)
    assert counts == {"node_document": 1}
    paths = {e.source_path for e in bundle.entities}
    assert paths == {"sop/boolean.txt"}


def test_hom_archive_dispatch_unchanged() -> None:
    docs = parse_archive_documents("hom.zip", [("hou/Node.txt", _HOM_TXT)])
    kinds = {e.kind for d in docs for e in d.entities}
    assert EntityKind.HOM_CLASS in kinds


def test_vex_archive_dispatch_unchanged() -> None:
    docs = parse_archive_documents("vex.zip", [("functions/intersect.txt", _VEX_TXT)])
    kinds = {e.kind for d in docs for e in d.entities}
    assert EntityKind.VEX_FUNCTION in kinds


def test_unknown_archive_type_produces_no_entity() -> None:
    docs = parse_archive_documents("hom.zip", [("hou/x.txt", b"#type: include\n= x =\n\"\"\"x.\"\"\"\n")])
    assert docs == []


# --- lock -----------------------------------------------------------------

def test_acquire_creates_lock_file_with_owner(tmp_path: Path) -> None:
    lock = tmp_path / "k.lock"
    acquire_build_lock(
        lock, pid=100, started_at=_dt(0), nonce="n1",
        pid_exists=lambda p: True, now=lambda: _dt(0), stale_ttl_seconds=100,
    )
    assert lock.is_file()
    data = json.loads(lock.read_text())
    assert data["pid"] == 100
    assert data["nonce"] == "n1"
    assert "started_at" in data


def test_active_lock_is_rejected(tmp_path: Path) -> None:
    lock = tmp_path / "k.lock"
    acquire_build_lock(
        lock, pid=100, started_at=_dt(0), nonce="n1",
        pid_exists=lambda p: True, now=lambda: _dt(0), stale_ttl_seconds=100,
    )
    with pytest.raises(LockBusy):
        acquire_build_lock(
            lock, pid=200, started_at=_dt(0), nonce="n2",
            pid_exists=lambda p: True, now=lambda: _dt(50), stale_ttl_seconds=100,
        )


def test_stale_lock_reclaimed_when_pid_gone_and_aged(tmp_path: Path) -> None:
    lock = tmp_path / "k.lock"
    acquire_build_lock(
        lock, pid=100, started_at=_dt(0), nonce="n1",
        pid_exists=lambda p: True, now=lambda: _dt(0), stale_ttl_seconds=100,
    )
    # pid 100 is now gone and 200s > 100s ttl -> reclaim.
    acquire_build_lock(
        lock, pid=200, started_at=_dt(200), nonce="n2",
        pid_exists=lambda p: False, now=lambda: _dt(200), stale_ttl_seconds=100,
    )
    data = json.loads(lock.read_text())
    assert data["pid"] == 200
    assert data["nonce"] == "n2"


def test_stale_lock_not_reclaimed_if_pid_alive(tmp_path: Path) -> None:
    lock = tmp_path / "k.lock"
    acquire_build_lock(
        lock, pid=100, started_at=_dt(0), nonce="n1",
        pid_exists=lambda p: True, now=lambda: _dt(0), stale_ttl_seconds=100,
    )
    with pytest.raises(LockBusy):
        acquire_build_lock(
            lock, pid=200, started_at=_dt(200), nonce="n2",
            pid_exists=lambda p: True, now=lambda: _dt(200), stale_ttl_seconds=100,
        )


def test_stale_lock_not_reclaimed_if_too_recent(tmp_path: Path) -> None:
    lock = tmp_path / "k.lock"
    acquire_build_lock(
        lock, pid=100, started_at=_dt(0), nonce="n1",
        pid_exists=lambda p: True, now=lambda: _dt(0), stale_ttl_seconds=100,
    )
    # pid gone but only 50s old (< 100s ttl) -> still busy.
    with pytest.raises(LockBusy):
        acquire_build_lock(
            lock, pid=200, started_at=_dt(50), nonce="n2",
            pid_exists=lambda p: False, now=lambda: _dt(50), stale_ttl_seconds=100,
        )


def test_release_removes_only_own_nonce(tmp_path: Path) -> None:
    lock = tmp_path / "k.lock"
    acquire_build_lock(
        lock, pid=100, started_at=_dt(0), nonce="n1",
        pid_exists=lambda p: True, now=lambda: _dt(0), stale_ttl_seconds=100,
    )
    release_build_lock(lock, nonce="other")
    assert lock.is_file()  # wrong nonce -> not removed
    release_build_lock(lock, nonce="n1")
    assert not lock.is_file()


# --- build ----------------------------------------------------------------

def test_build_selftest_writes_valid_cache(tmp_path: Path) -> None:
    out = tmp_path / "knowledge.sqlite3"
    manifest = build_cache(
        BuildOptions(output=out, selftest=True),
        now=lambda: _dt(0), pid_exists=lambda p: False,
    )
    assert out.is_file()
    validate_cache(out)  # build self-check passes
    assert manifest.kb_schema_version == 1
    assert manifest.houdini_version == "21.0.440"
    assert manifest.houdini_build == "21.0.440"
    assert manifest.node_inventory_sha256  # inventory hash recorded


def test_build_atomic_no_leftover_temps_and_lock_released(tmp_path: Path) -> None:
    out = tmp_path / "knowledge.sqlite3"
    build_cache(
        BuildOptions(output=out, selftest=True),
        now=lambda: _dt(0), pid_exists=lambda p: False,
    )
    assert not list(tmp_path.glob("knowledge.sqlite3.tmp.*"))
    assert not (tmp_path / "knowledge.sqlite3.lock").exists()


def test_failed_build_preserves_previous_cache(tmp_path: Path) -> None:
    final = tmp_path / "knowledge.sqlite3"
    final.write_bytes(b"previous-valid-cache")

    def raising_writer(path, bundle, manifest):
        Path(path).write_bytes(b"partial-temp")
        raise BuildError("simulated writer failure")

    with pytest.raises(BuildError):
        build_cache(
            BuildOptions(output=final, selftest=True),
            now=lambda: _dt(0), pid_exists=lambda p: False,
            writer=raising_writer,
        )
    assert final.read_bytes() == b"previous-valid-cache"
    assert not list(tmp_path.glob("knowledge.sqlite3.tmp.*"))


def test_failed_build_releases_lock(tmp_path: Path) -> None:
    out = tmp_path / "knowledge.sqlite3"

    def raising_writer(path, bundle, manifest):
        raise BuildError("boom")

    with pytest.raises(BuildError):
        build_cache(
            BuildOptions(output=out, selftest=True),
            now=lambda: _dt(0), pid_exists=lambda p: False,
            writer=raising_writer,
        )
    assert not (tmp_path / "knowledge.sqlite3.lock").exists()


def test_active_lock_blocks_build(tmp_path: Path) -> None:
    out = tmp_path / "knowledge.sqlite3"
    lock = lock_path_for(out)
    acquire_build_lock(
        lock, pid=999, started_at=_dt(0), nonce="other",
        pid_exists=lambda p: True, now=lambda: _dt(0),
        stale_ttl_seconds=LOCK_STALE_TTL_SECONDS,
    )
    with pytest.raises(BuildError):
        build_cache(
            BuildOptions(output=out, selftest=True),
            now=lambda: _dt(0), pid_exists=lambda p: True,
        )
    assert not out.exists()
    # The other build's lock must not have been deleted.
    assert lock.is_file()


def test_build_manifest_records_real_version_and_inventory_hash(tmp_path: Path) -> None:
    out = tmp_path / "knowledge.sqlite3"
    manifest = build_cache(
        BuildOptions(output=out, selftest=True),
        now=lambda: _dt(0), pid_exists=lambda p: False,
    )
    from eee_agent.knowledge.manifest import hash_inventory
    assert manifest.node_inventory_sha256 == hash_inventory(frozenset({"boolean"}))
    assert manifest.houdini_version == manifest.houdini_build == "21.0.440"


# --- main / --selftest ----------------------------------------------------

def test_main_selftest_returns_json_summary(capsys) -> None:
    rc = main(["--selftest"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["selftest"] is True
    assert data["kb_schema_version"] == 1
    assert "manifest_sha256" in data
    assert data["entity_count"] > 0
    assert data["edge_count"] > 0
    assert data["houdini_build"] == "21.0.440"


def test_main_selftest_leaves_no_cache_in_cwd(capsys) -> None:
    before = set(Path.cwd().glob("*.sqlite3"))
    rc = main(["--selftest"])
    assert rc == 0
    capsys.readouterr()
    after = set(Path.cwd().glob("*.sqlite3"))
    assert before == after


def test_main_real_build_requires_out(capsys) -> None:
    rc = main([])
    assert rc != 0
    err = capsys.readouterr().err
    assert json.loads(err)["ok"] is False
