from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Mapping

from eee_agent.runtime.artifacts import ArtifactStore
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.vision import (
    ProviderCapability,
    VisionRequest,
    VisionRouter,
    VisionStatus,
)


SES = "ses_" + "1" * 32
RUN = "run_" + "2" * 32
ART = "art_" + "3" * 32
IMAGE = b"\x89PNG\r\n\x1a\nverified-vision-bytes"


class FakeProvider:
    def __init__(self) -> None:
        self.capability_result: object = ProviderCapability(
            "fake", True, ("image/png",), 4096, None
        )
        self.response: object = {
            "summary": "silhouette matches",
            "observations": ["bounded observation"],
            "confidence": 0.9,
            "advisory_passed": True,
        }
        self.delay = 0.0
        self.received: list[bytes] = []

    async def capability(self) -> ProviderCapability:
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.capability_result, BaseException):
            raise self.capability_result
        return self.capability_result  # type: ignore[return-value]

    async def evaluate(
        self, request: VisionRequest, image_bytes: bytes
    ) -> Mapping[str, object]:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.received.append(image_bytes)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response  # type: ignore[return-value]


async def _store(tmp_path: Path):
    db = await RuntimeDatabase.open(tmp_path / "runtime.sqlite")
    async with db.write_transaction() as conn:
        await conn.execute(
            "INSERT INTO sessions(session_id,title,status,created_at,updated_at,last_seq,replay_floor_seq) VALUES (?,?,?,?,?,?,?)",
            (SES, "Vision", "active", "2026-07-20T00:00:00+00:00", "2026-07-20T00:00:00+00:00", 0, 0),
        )
        await conn.execute(
            "INSERT INTO runs(run_id,session_id,status,user_input,final_response,created_at,started_at,finished_at,failure_json,model_snapshot_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (RUN, SES, "Completed", "vision", None, "2026-07-20T00:00:00+00:00", None, "2026-07-20T00:00:00+00:00", None, "{}"),
        )
    store = ArtifactStore(db, tmp_path / "artifacts")
    source = tmp_path / "preview.png"
    source.write_bytes(IMAGE)
    ref = await store.register(
        session_id=SES,
        run_id=RUN,
        artifact_id=ART,
        source=source,
        media_type="image/png",
        expected_sha256=hashlib.sha256(IMAGE).hexdigest(),
        expected_size_bytes=len(IMAGE),
    )
    return db, store, ref


def test_router_passes_exact_verified_artifact_bytes(tmp_path: Path) -> None:
    async def scenario() -> None:
        db, store, ref = await _store(tmp_path)
        provider = FakeProvider()
        try:
            outcome = await VisionRouter(store, provider).evaluate(
                VisionRequest("vision-1", ref, "check"), deterministic_valid=True
            )
            assert provider.received == [IMAGE]
            assert outcome.decision.accepted
            assert outcome.report is not None
        finally:
            await db.close()

    asyncio.run(scenario())


def test_provider_unavailable_and_api_key_missing_are_explicit(tmp_path: Path) -> None:
    async def scenario() -> None:
        db, store, ref = await _store(tmp_path)
        request = VisionRequest("vision-1", ref, "check")
        try:
            missing = await VisionRouter(store, None).evaluate(
                request, deterministic_valid=True
            )
            assert missing.unavailable is not None
            assert missing.unavailable.reason_code == "vision.provider_unavailable"

            provider = FakeProvider()
            provider.capability_result = ProviderCapability(
                "fake", False, ("image/png",), 4096, "vision.api_key_missing"
            )
            no_key = await VisionRouter(store, provider).evaluate(
                request, deterministic_valid=True
            )
            assert no_key.unavailable is not None
            assert no_key.unavailable.reason_code == "vision.api_key_missing"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_timeout_and_schema_invalid_response_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        db, store, ref = await _store(tmp_path)
        request = VisionRequest("vision-1", ref, "check")
        try:
            slow = FakeProvider()
            slow.delay = 0.2
            timeout = await VisionRouter(store, slow, timeout_seconds=0.1).evaluate(
                request, deterministic_valid=True
            )
            assert timeout.unavailable is not None
            assert timeout.unavailable.reason_code == "vision.provider_timeout"

            malformed = FakeProvider()
            malformed.response = {"summary": "unbounded schema"}
            invalid = await VisionRouter(store, malformed).evaluate(
                request, deterministic_valid=True
            )
            assert invalid.unavailable is not None
            assert invalid.unavailable.reason_code == "vision.response_invalid"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_waiver_does_not_call_provider(tmp_path: Path) -> None:
    async def scenario() -> None:
        db, store, ref = await _store(tmp_path)
        provider = FakeProvider()
        try:
            outcome = await VisionRouter(store, provider).evaluate(
                VisionRequest("vision-1", ref, "check"),
                deterministic_valid=True,
                waived=True,
            )
            assert outcome.decision.status is VisionStatus.WAIVED
            assert outcome.decision.accepted
            assert provider.received == []
        finally:
            await db.close()

    asyncio.run(scenario())


def test_missing_and_changed_artifact_never_reaches_provider(tmp_path: Path) -> None:
    async def scenario() -> None:
        db, store, ref = await _store(tmp_path)
        provider = FakeProvider()
        try:
            changed = ref.__class__(
                artifact_id=ref.artifact_id,
                relative_path=ref.relative_path,
                sha256="b" * 64,
                media_type=ref.media_type,
                size_bytes=ref.size_bytes,
            )
            unavailable = await VisionRouter(store, provider).evaluate(
                VisionRequest("vision-1", changed, "check"), deterministic_valid=True
            )
            assert unavailable.unavailable is not None
            assert unavailable.unavailable.reason_code == "vision.artifact_unavailable"

            store.path_for(ref).write_bytes(b"changed")
            corrupt = await VisionRouter(store, provider).evaluate(
                VisionRequest("vision-2", ref, "check"), deterministic_valid=True
            )
            assert corrupt.unavailable is not None
            assert corrupt.unavailable.reason_code == "vision.artifact_integrity_failed"
            assert provider.received == []
        finally:
            await db.close()

    asyncio.run(scenario())


def test_artifact_growth_is_bounded_before_provider_call(tmp_path: Path) -> None:
    async def scenario() -> None:
        db, store, ref = await _store(tmp_path)
        provider = FakeProvider()
        provider.capability_result = ProviderCapability(
            "fake", True, ("image/png",), len(IMAGE), None
        )
        try:
            store.path_for(ref).write_bytes(IMAGE + b"unbounded-tail")
            outcome = await VisionRouter(store, provider).evaluate(
                VisionRequest("vision-1", ref, "check"), deterministic_valid=True
            )
            assert outcome.unavailable is not None
            assert outcome.unavailable.reason_code == "vision.artifact_integrity_failed"
            assert provider.received == []
        finally:
            await db.close()

    asyncio.run(scenario())


def test_advisory_success_cannot_override_deterministic_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        db, store, ref = await _store(tmp_path)
        try:
            outcome = await VisionRouter(store, FakeProvider()).evaluate(
                VisionRequest("vision-1", ref, "check"), deterministic_valid=False
            )
            assert outcome.report is not None and outcome.report.advisory_passed
            assert not outcome.decision.accepted
            assert not outcome.decision.deterministic_valid
        finally:
            await db.close()

    asyncio.run(scenario())
