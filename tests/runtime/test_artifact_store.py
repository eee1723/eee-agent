"""Task 19-A: content-addressed artifact store tests.

Drives :class:`eee_agent.runtime.artifacts.ArtifactStore` against a real
``RuntimeDatabase`` (tmp_path) and a real artifacts directory: registration
re-hash verification, hash/size mismatch fail-closed deletion, canonical
placement, lookup/list, restart discoverability, deterministic oldest-first
retention (per-session count/bytes and global caps), and session cleanup.
Async scenarios run via ``asyncio.run`` (no pytest-asyncio, matching the rest
of the Runtime tests).
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path

import pytest

from eee_agent.core import AgentException
from eee_agent.core.artifacts import ArtifactRef
from eee_agent.core.ids import IdKind, new_id
from eee_agent.runtime.artifacts import (
    MAX_ARTIFACTS_PER_SESSION,
    ArtifactStore,
)
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.panel.runtime_state import parse_artifact_event

SES = f"ses_{'0' * 32}"
SES2 = f"ses_{'1' * 32}"
RUN = f"run_{'2' * 32}"
RUN2 = f"run_{'3' * 32}"
NOW = "2026-07-17T08:00:00+00:00"

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(range(32))


def _png(payload: bytes = _PNG) -> bytes:
    return payload


async def _seed_session_run(db: RuntimeDatabase, session: str = SES, run: str = RUN) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO sessions(session_id,title,status,created_at,updated_at,last_seq,replay_floor_seq) "
            "VALUES (?,?,?,?,?,?,?)",
            (session, "T", "active", NOW, NOW, 0, 0),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO runs(run_id,session_id,status,user_input,final_response,created_at,"
            "started_at,finished_at,failure_json,model_snapshot_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run, session, "Completed", "x", None, NOW, None, NOW, None, "{}"),
        )


def _write_capture(root: Path, session: str, run: str, artifact_id: str, payload: bytes) -> Path:
    target = root / session / run
    target.mkdir(parents=True, exist_ok=True)
    source = target / f"{artifact_id}.png"
    source.write_bytes(payload)
    return source


async def _register(
    store: ArtifactStore,
    root: Path,
    *,
    session: str = SES,
    run: str = RUN,
    artifact_id: str | None = None,
    payload: bytes = _PNG,
) -> ArtifactRef:
    artifact_id = artifact_id or new_id(IdKind.ARTIFACT)
    source = _write_capture(root, session, run, artifact_id, payload)
    return await store.register(
        session_id=session,
        run_id=run,
        artifact_id=artifact_id,
        source=source,
        media_type="image/png",
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_size_bytes=len(payload),
    )


# --------------------------------------------------------------------------
# registration + verification
# --------------------------------------------------------------------------


def test_register_verifies_places_and_returns_ref(db_path: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            artifact_id = new_id(IdKind.ARTIFACT)
            ref = await _register(store, root, artifact_id=artifact_id)
            assert ref.artifact_id == artifact_id
            assert ref.relative_path == f"{SES}/{RUN}/{artifact_id}.png"
            assert ref.sha256 == hashlib.sha256(_PNG).hexdigest()
            assert ref.size_bytes == len(_PNG)
            assert ref.media_type == "image/png"
            assert ref.schema_version == 1
            placed = store.path_for(ref)
            assert placed.is_file()
            assert placed.read_bytes() == _PNG
            assert placed == root / SES / RUN / f"{artifact_id}.png"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_register_hash_mismatch_deletes_file_and_never_registers(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            artifact_id = new_id(IdKind.ARTIFACT)
            source = _write_capture(root, SES, RUN, artifact_id, _PNG)
            with pytest.raises(AgentException) as exc:
                await store.register(
                    session_id=SES,
                    run_id=RUN,
                    artifact_id=artifact_id,
                    source=source,
                    media_type="image/png",
                    expected_sha256="0" * 64,
                    expected_size_bytes=len(_PNG),
                )
            assert exc.value.error.code == "runtime.artifact_hash_mismatch"
            assert not source.exists()
            assert await store.get(artifact_id) is None
        finally:
            await db.close()

    asyncio.run(scenario())


def test_register_size_mismatch_deletes_file(db_path: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            artifact_id = new_id(IdKind.ARTIFACT)
            source = _write_capture(root, SES, RUN, artifact_id, _PNG)
            with pytest.raises(AgentException) as exc:
                await store.register(
                    session_id=SES,
                    run_id=RUN,
                    artifact_id=artifact_id,
                    source=source,
                    media_type="image/png",
                    expected_sha256=hashlib.sha256(_PNG).hexdigest(),
                    expected_size_bytes=len(_PNG) + 1,
                )
            assert exc.value.error.code == "runtime.artifact_hash_mismatch"
            assert not source.exists()
        finally:
            await db.close()

    asyncio.run(scenario())


def test_register_missing_file_fails_closed(db_path: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            store = ArtifactStore(db, tmp_path / "artifacts")
            with pytest.raises(AgentException) as exc:
                await store.register(
                    session_id=SES,
                    run_id=RUN,
                    artifact_id=new_id(IdKind.ARTIFACT),
                    source=tmp_path / "ghost.png",
                    media_type="image/png",
                    expected_sha256="0" * 64,
                    expected_size_bytes=1,
                )
            assert exc.value.error.code == "runtime.artifact_missing"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_register_moves_staged_file_into_canonical_location(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            artifact_id = new_id(IdKind.ARTIFACT)
            staged = tmp_path / "staging" / f"{artifact_id}.png"
            staged.parent.mkdir(parents=True)
            staged.write_bytes(_PNG)
            ref = await store.register(
                session_id=SES,
                run_id=RUN,
                artifact_id=artifact_id,
                source=staged,
                media_type="image/png",
                expected_sha256=hashlib.sha256(_PNG).hexdigest(),
                expected_size_bytes=len(_PNG),
            )
            assert not staged.exists()
            assert (root / SES / RUN / f"{artifact_id}.png").read_bytes() == _PNG
            assert ref.relative_path == f"{SES}/{RUN}/{artifact_id}.png"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_duplicate_artifact_id_is_rejected(db_path: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            artifact_id = new_id(IdKind.ARTIFACT)
            await _register(store, root, artifact_id=artifact_id)
            with pytest.raises(AgentException) as exc:
                await _register(store, root, artifact_id=artifact_id)
            assert exc.value.error.code == "runtime.artifact_invalid"
        finally:
            await db.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# lookup / list / restart
# --------------------------------------------------------------------------


def test_lookup_and_list_by_session_and_run(db_path: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            await _seed_session_run(db, SES, RUN2)
            await _seed_session_run(db, SES2, RUN2)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            first = await _register(store, root)
            second = await _register(store, root, run=RUN2)
            third = await _register(store, root, session=SES2, run=RUN2)
            assert (await store.get(first.artifact_id)) == first
            assert await store.get(new_id(IdKind.ARTIFACT)) is None
            session_items = await store.list_for_session(SES)
            assert {item.artifact_id for item in session_items} == {
                first.artifact_id,
                second.artifact_id,
            }
            run_items = await store.list_for_run(SES, RUN2)
            assert [item.artifact_id for item in run_items] == [second.artifact_id]
            other_items = await store.list_for_session(SES2)
            assert [item.artifact_id for item in other_items] == [third.artifact_id]
            with pytest.raises(ValueError):
                await store.list_for_session(SES, limit=0)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_artifacts_survive_restart(db_path: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        root = tmp_path / "artifacts"
        await _seed_session_run(db)
        ref = await _register(ArtifactStore(db, root), root)
        await db.close()
        # A fresh process re-opens the same database: the record and the bytes
        # are still discoverable (no replay, no re-registration).
        db2 = await RuntimeDatabase.open(db_path)
        try:
            store2 = ArtifactStore(db2, root)
            found = await store2.get(ref.artifact_id)
            assert found == ref
            assert store2.path_for(found).read_bytes() == _PNG  # type: ignore[arg-type]
            assert len(await store2.list_for_session(SES)) == 1
        finally:
            await db2.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# bounded retention (deterministic oldest-first eviction)
# --------------------------------------------------------------------------


def test_session_count_cap_evicts_oldest(db_path: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            refs = []
            for _ in range(MAX_ARTIFACTS_PER_SESSION + 2):
                refs.append(await _register(store, root))
            items = await store.list_for_session(SES, limit=256)
            assert len(items) == MAX_ARTIFACTS_PER_SESSION
            # The two oldest were evicted: rows AND files are gone.
            for evicted in refs[:2]:
                assert await store.get(evicted.artifact_id) is None
                assert not store.path_for(evicted).exists()
            for kept in refs[2:]:
                assert store.path_for(kept).is_file()
        finally:
            await db.close()

    asyncio.run(scenario())


def test_session_byte_cap_evicts_oldest(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eee_agent.runtime.artifacts.MAX_SESSION_BYTES", 4 * len(_PNG))

    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            refs = []
            for _ in range(6):
                refs.append(await _register(store, root))
            items = await store.list_for_session(SES, limit=256)
            # 6 files of len(_PNG): byte cap 4 keeps at most the newest 4; the
            # count cap (32) does not bind here.
            assert len(items) == 4
            for evicted in refs[:2]:
                assert await store.get(evicted.artifact_id) is None
                assert not store.path_for(evicted).exists()
        finally:
            await db.close()

    asyncio.run(scenario())


def test_global_count_cap_spans_sessions(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("eee_agent.runtime.artifacts.MAX_ARTIFACTS_GLOBAL", 4)

    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            await _seed_session_run(db, SES2, RUN2)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            refs = []
            for session, run in ((SES, RUN), (SES, RUN), (SES2, RUN2), (SES2, RUN2), (SES2, RUN2)):
                refs.append(await _register(store, root, session=session, run=run))
            # Global cap 4: the single oldest (first SES artifact) was evicted
            # even though the per-session caps were not exceeded.
            assert await store.get(refs[0].artifact_id) is None
            assert not store.path_for(refs[0]).exists()
            assert len(await store.list_for_session(SES)) == 1
            assert len(await store.list_for_session(SES2)) == 3
        finally:
            await db.close()

    asyncio.run(scenario())


def test_retention_never_evicts_the_just_registered_artifact(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("eee_agent.runtime.artifacts.MAX_SESSION_BYTES", 1)

    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            first = await _register(store, root)
            second = await _register(store, root)
            # The byte cap of 1 cannot be met by eviction alone; the freshest
            # artifact is never its own eviction candidate.
            assert await store.get(first.artifact_id) is None
            assert (await store.get(second.artifact_id)) == second
            assert store.path_for(second).is_file()
        finally:
            await db.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# session cleanup
# --------------------------------------------------------------------------


def test_delete_session_artifacts_removes_files_and_rows(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            await _seed_session_run(db, SES2, RUN2)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            await _register(store, root)
            await _register(store, root, session=SES2, run=RUN2)
            removed = await store.delete_session_artifacts(SES)
            assert removed == 1
            assert not (root / SES).exists()
            assert (root / SES2).is_dir()
            assert await store.list_for_session(SES) == ()
            assert len(await store.list_for_session(SES2)) == 1
            # Idempotent on a missing directory.
            assert await store.delete_session_artifacts(SES) == 0
        finally:
            await db.close()

    asyncio.run(scenario())


def test_session_row_delete_cascades_artifact_rows(db_path: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            ref = await _register(store, root)
            async with db.write_transaction() as conn:
                await conn.execute("DELETE FROM sessions WHERE session_id = ?", (SES,))
            assert await store.get(ref.artifact_id) is None
        finally:
            await db.close()

    asyncio.run(scenario())


def test_artifacts_table_shape(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            names = await db.table_names()
            assert "artifacts" in names
            cols = await db.fetchall("PRAGMA table_info(artifacts)")
            assert {row["name"] for row in cols} == {
                "artifact_id",
                "session_id",
                "run_id",
                "relative_path",
                "sha256",
                "media_type",
                "size_bytes",
                "redacted",
                "created_at",
                "schema_version",
                "artifact_state",
                "cleanup_attempts",
                "last_error_code",
                "updated_at",
            }
        finally:
            await db.close()

    asyncio.run(scenario())


def test_retention_db_rollback_does_not_restore_missing_file(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-unlink failure cannot leave an available row for a gone file."""
    monkeypatch.setattr("eee_agent.runtime.artifacts.MAX_SESSION_BYTES", len(_PNG))
    original_unlink = Path.unlink

    def unlink_then_fail(path: Path, *args, **kwargs):
        original_unlink(path, *args, **kwargs)
        if path.name.endswith(".png"):
            raise RuntimeError("injected failure after eviction unlink")

    monkeypatch.setattr(Path, "unlink", unlink_then_fail)
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            first = await _register(store, root)
            with pytest.raises(RuntimeError):
                await _register(store, root)
            row = await db.fetchone(
                "SELECT artifact_state FROM artifacts WHERE artifact_id = ?",
                (first.artifact_id,),
            )
            assert row is not None
            assert not store.path_for(first).exists()
            assert row["artifact_state"] == "pending_eviction"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_register_commit_failure_leaves_recoverable_pending_record(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            original = store._mark_available

            async def fail_commit(*args, **kwargs):
                raise RuntimeError("injected available commit failure")

            monkeypatch.setattr(store, "_mark_available", fail_commit)
            with pytest.raises(RuntimeError):
                await _register(store, root)
            rows = await db.fetchall(
                "SELECT artifact_state FROM artifacts ORDER BY created_at"
            )
            assert rows and rows[-1]["artifact_state"] in {"pending", "failed"}
            monkeypatch.setattr(store, "_mark_available", original)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_restart_reconciles_pending_file_placement(db_path: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            payload = _PNG
            artifact_id = new_id(IdKind.ARTIFACT)
            staged = root / ".staging" / f"{artifact_id}-restart.stage"
            staged.parent.mkdir(parents=True)
            staged.write_bytes(payload)
            rel = f"{SES}/{RUN}/{artifact_id}.png"
            async with db.write_transaction() as conn:
                await conn.execute(
                    "INSERT INTO artifacts(artifact_id,session_id,run_id,relative_path,sha256,media_type,size_bytes,redacted,created_at,schema_version,artifact_state,cleanup_attempts,last_error_code,updated_at) VALUES (?,?,?,?,?,?,?,0,?,1,'pending',0,NULL,?)",
                    (artifact_id, SES, RUN, rel, hashlib.sha256(payload).hexdigest(), "image/png", len(payload), NOW, NOW),
                )
            await store.reconcile()
            ref = await store.get(artifact_id)
            assert ref is not None and store.path_for(ref).read_bytes() == payload
        finally:
            await db.close()

    asyncio.run(scenario())


def test_restart_marks_missing_available_file_without_success_event(db_path: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            ref = await _register(store, root)
            store.path_for(ref).unlink()
            await store.reconcile()
            row = await db.fetchone("SELECT artifact_state FROM artifacts WHERE artifact_id = ?", (ref.artifact_id,))
            assert row["artifact_state"] == "missing"
            assert await store.get(ref.artifact_id) is None
        finally:
            await db.close()

    asyncio.run(scenario())


def test_cleanup_failure_is_retryable_after_session_delete(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            ref = await _register(store, root)
            original = shutil.rmtree
            monkeypatch.setattr("eee_agent.runtime.artifacts.shutil.rmtree", lambda _: (_ for _ in ()).throw(OSError("busy")))
            with pytest.raises(OSError):
                await store.delete_session_artifacts(SES)
            row = await db.fetchone("SELECT artifact_state, cleanup_attempts FROM artifacts WHERE artifact_id = ?", (ref.artifact_id,))
            assert row["artifact_state"] == "pending_eviction" and row["cleanup_attempts"] >= 1
            monkeypatch.setattr("eee_agent.runtime.artifacts.shutil.rmtree", original)
            assert await store.delete_session_artifacts(SES) == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_event_replay_distinguishes_evicted_artifact(db_path: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            ref = await _register(store, root)
            async with db.write_transaction() as conn:
                await conn.execute("UPDATE artifacts SET artifact_state='evicted' WHERE artifact_id=?", (ref.artifact_id,))
            assert await store.get(ref.artifact_id) is None
            row = await db.fetchone("SELECT artifact_state FROM artifacts WHERE artifact_id=?", (ref.artifact_id,))
            assert row["artifact_state"] == "evicted"
            summary = parse_artifact_event(
                {
                    "kind": "event",
                    "type": "modeling.artifact_state_changed",
                    "seq": 1,
                    "payload": {"artifact_id": ref.artifact_id, "state": "evicted"},
                }
            )
            assert summary["kind"] == "lifecycle"
            assert summary["viewable"] is False
        finally:
            await db.close()

    asyncio.run(scenario())


def test_duplicate_artifact_path_never_overwrites_available_bytes(db_path: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            store = ArtifactStore(db, root)
            first = await _register(store, root, artifact_id=new_id(IdKind.ARTIFACT))
            source = root / "incoming" / Path(first.relative_path).name
            source.parent.mkdir(parents=True)
            source.write_bytes(b"different")
            with pytest.raises(AgentException):
                await store.register(session_id=SES, run_id=RUN, artifact_id=new_id(IdKind.ARTIFACT), source=source, media_type="image/png", expected_sha256=hashlib.sha256(b"different").hexdigest(), expected_size_bytes=len(b"different"))
            assert store.path_for(first).read_bytes() == _PNG
        finally:
            await db.close()

    asyncio.run(scenario())


def test_reconcile_removes_orphan_staging_file(db_path: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await _seed_session_run(db)
            root = tmp_path / "artifacts"
            orphan = root / ".staging" / "art_deadbeef.stage"
            orphan.parent.mkdir(parents=True)
            orphan.write_bytes(_PNG)
            await ArtifactStore(db, root).reconcile()
            assert not orphan.exists()
        finally:
            await db.close()

    asyncio.run(scenario())
