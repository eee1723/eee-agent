from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from eee_agent.knowledge.service import KnowledgeService
from eee_agent.runtime.agent_tools import build_read_only_tools
from eee_agent.runtime.knowledge import KnowledgeRuntime, KnowledgeStatusCode


def test_runtime_readonly_allowlist_includes_knowledge_tools() -> None:
    names = {tool.name for tool in build_read_only_tools()}
    assert {"search_houdini_knowledge", "get_houdini_knowledge"} <= names


def test_knowledge_query_never_imports_legacy_bridge() -> None:
    source = inspect.getsource(KnowledgeService)
    assert "eee_agent.bridge" not in source
    assert "eee_agent.tools" not in source
    assert "sqlite3.connect" not in source


def test_missing_cache_emits_nonblocking_status(tmp_path: Path) -> None:
    runtime = KnowledgeRuntime(tmp_path / "missing.sqlite")
    assert runtime.status.code is KnowledgeStatusCode.MISSING
    assert runtime.status.available is False
    assert runtime.search("noise")["code"] == "kb_unavailable"


def test_stale_cache_emits_status_and_run_snapshot(tmp_path: Path) -> None:
    runtime = KnowledgeRuntime(tmp_path / "cache.sqlite", stale_checker=lambda _: True)
    runtime._status = runtime._status.__class__(
        code=KnowledgeStatusCode.STALE,
        available=False,
        manifest_sha256="a" * 64,
        schema_version=1,
        houdini_build="H21.0",
        message="stale",
    )
    assert runtime.status.code is KnowledgeStatusCode.STALE
    snapshot = runtime.snapshot_fields()
    assert snapshot["knowledge_status"] == "stale"
    assert snapshot["kb_manifest_sha256"] == "a" * 64


def test_corrupt_cache_does_not_block_runtime_start(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite"
    path.write_bytes(b"not sqlite")
    runtime = KnowledgeRuntime(path)
    assert runtime.status.code is KnowledgeStatusCode.CORRUPT
    assert runtime.startup_allowed is True


def test_run_snapshot_contains_kb_manifest_sha256_schema_and_houdini_build(
    tmp_path: Path,
) -> None:
    runtime = KnowledgeRuntime(tmp_path / "cache.sqlite")
    runtime._status = runtime._status.__class__(
        code=KnowledgeStatusCode.READY,
        available=True,
        manifest_sha256="b" * 64,
        schema_version=3,
        houdini_build="21.0.383",
        message="ready",
    )
    assert runtime.snapshot_fields() == {
        "kb_manifest_sha256": "b" * 64,
        "kb_schema_version": 3,
        "houdini_build": "21.0.383",
        "knowledge_status": "ready",
    }


def test_knowledge_body_query_is_bounded_and_logical_path_only(tmp_path: Path) -> None:
    runtime = KnowledgeRuntime(tmp_path / "missing.sqlite")
    response = runtime.get("../../secret", max_body_bytes=100_000)
    assert response["code"] == "kb_unavailable"
    assert len(response.get("body", "")) <= 8_000


@pytest.mark.parametrize("limit", ["5", None, True, False, -1, 11])
def test_ready_knowledge_search_rejects_invalid_limits_without_raising(
    tmp_path: Path, limit: object
) -> None:
    runtime = KnowledgeRuntime(tmp_path / "cache.sqlite")
    runtime._status = runtime._status.__class__(
        code=KnowledgeStatusCode.READY,
        available=True,
        message="ready",
    )

    result = runtime.search("nodes", limit=limit)  # type: ignore[arg-type]

    assert result == {
        "ok": False,
        "code": "kb_invalid_input",
        "message": "knowledge search limit is invalid",
    }


@pytest.mark.parametrize("max_body_bytes", ["4000", None, True, False, -1, 8_001])
def test_ready_knowledge_get_rejects_invalid_body_limits_without_raising(
    tmp_path: Path, max_body_bytes: object
) -> None:
    runtime = KnowledgeRuntime(tmp_path / "cache.sqlite")
    runtime._status = runtime._status.__class__(
        code=KnowledgeStatusCode.READY,
        available=True,
        message="ready",
    )

    result = runtime.get(  # type: ignore[arg-type]
        "entity", max_body_bytes=max_body_bytes
    )

    assert result == {
        "ok": False,
        "code": "kb_invalid_input",
        "message": "knowledge body limit is invalid",
    }


def test_live_catalog_remains_authority_for_creatability() -> None:
    runtime = KnowledgeRuntime(Path("missing.sqlite"))
    assert runtime.can_create("geo") is False
    assert runtime.can_create("/obj/geo1") is False
