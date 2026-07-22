"""Production post-Apply advisory Vision wiring tests.

Prove the RuntimeService production flow end to end: a real Apply capture is
registered in the ArtifactStore, the exact verified bytes reach the injected
VisionProvider, and a semantically consistent DeliveryEvaluation is persisted
as a durable, replayable ``vision.evaluation_completed`` event. Advisory
Vision can never replay, undo, or fail the already-durable Apply, and it can
never turn a failed deterministic report into ``accepted=true``.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Mapping

from eee_agent.changesets.repository import ChangeSetState
from eee_agent.houdini_bridge.contracts import SceneQueryResult
from eee_agent.modeling.catalog import houdini_21_minimal_catalog
from eee_agent.runtime.paths import RuntimePaths
from eee_agent.runtime.service import RuntimeService
from eee_agent.vision import ProviderCapability, VisionRequest

from tests.runtime.test_changeset_recovery import (
    _CAPTURE_PNG,
    _binding,
    _box_changeset,
    _seed,
    CHG,
    NOW,
    FakeBridgeWithCapture,
)


class FakeVisionProvider:
    """Deterministic VisionProvider double recording exactly what it gets."""

    def __init__(self) -> None:
        self.capability_result: object = ProviderCapability(
            "fake-vision", True, ("image/png",), 4096, None
        )
        self.response: object = {
            "summary": "silhouette matches",
            "observations": ["bounded observation"],
            "confidence": 0.9,
            "advisory_passed": True,
        }
        self.requests: list[VisionRequest] = []
        self.received: list[bytes] = []

    async def capability(self) -> ProviderCapability:
        if isinstance(self.capability_result, BaseException):
            raise self.capability_result
        return self.capability_result  # type: ignore[return-value]

    async def evaluate(
        self, request: VisionRequest, image_bytes: bytes
    ) -> Mapping[str, object]:
        self.requests.append(request)
        self.received.append(image_bytes)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response  # type: ignore[return-value]


class FailingGeometryBridge(FakeBridgeWithCapture):
    """Capture succeeds, but the deterministic scene validators fail."""

    async def inspect_geometry(self, changeset):
        self.calls.append("inspect_geometry")
        return SceneQueryResult(binding=_binding(), selected_nodes=(), nodes=())


def _paths(db_path: Path, tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        home=tmp_path,
        state_dir=tmp_path,
        app_db=db_path,
        checkpoints_db=tmp_path / "checkpoints.sqlite",
        lock_file=tmp_path / "runtime.lock",
        discovery_file=tmp_path / "runtime.json",
        token_file=tmp_path / "runtime.token",
        artifacts_dir=tmp_path / "artifacts",
    )


def _service(db, paths: RuntimePaths, bridge, vision=None) -> RuntimeService:
    return RuntimeService(
        db,
        paths,
        changeset_clock=lambda: NOW,
        changeset_bridge_provider=bridge,
        modeling_catalog_provider=houdini_21_minimal_catalog,
        vision_provider=vision,
    )


def _vision_events(replay) -> list:
    return [
        event
        for event in replay.events
        if event.event_type == "vision.evaluation_completed"
    ]


def test_apply_capture_flows_exact_bytes_through_vision_to_durable_replay(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db, events, _repo, _changesets, _seed_bridge = await _seed(
            db_path, changeset=_box_changeset()
        )
        bridge = FakeBridgeWithCapture()
        provider = FakeVisionProvider()
        runtime = _service(db, _paths(db_path, tmp_path), bridge, provider)
        try:
            result = await runtime.apply_changeset_trusted(CHG)
            assert result.state is ChangeSetState.APPLIED
            # The provider received exactly the ArtifactStore-verified bytes.
            assert provider.received == [_CAPTURE_PNG]
            assert len(provider.requests) == 1
            request = provider.requests[0]
            artifact = request.artifact
            assert artifact.sha256 == hashlib.sha256(_CAPTURE_PNG).hexdigest()
            assert artifact.media_type == "image/png"
            # The request ref is the registered store record, not a Bridge path.
            stored = await runtime._artifacts.get(artifact.artifact_id)
            assert stored is not None and stored == artifact

            replay = await events.replay(
                result.changeset.session_id, after_seq=0, limit=100
            )
            ordered = [event.event_type for event in replay.events]
            # The deterministic validation event is durable BEFORE Vision.
            assert ordered.index("modeling.validation_completed") < ordered.index(
                "vision.evaluation_completed"
            )
            (vision_event,) = _vision_events(replay)
            payload = vision_event.payload
            assert payload["changeset_digest"] == result.changeset.digest
            assert payload["brief"] == "x"
            assert payload["spec"].startswith("operations=1; effects=parm.set")
            assert payload["approval"].startswith("Consumed:local_user:")
            assert payload["receipt"] == (
                f"{result.receipt.status.value}:{result.receipt.after_revision}"
            )
            assert payload["knowledge_manifest_sha256"] is None
            assert any(
                item.startswith("Artifact:Passed:")
                for item in payload["validation_report"]
            )
            assert [ref["sha256"] for ref in payload["artifact_refs"]] == [
                artifact.sha256
            ]
            assert list(payload["artifact_status"]) == ["available"]
            assert payload["vision_status"] == "completed"
            assert payload["vision_report"]["advisory_passed"] is True
            decision = payload["final_decision"]
            assert decision["status"] == "completed"
            assert decision["deterministic_valid"] is True
            assert decision["accepted"] is True

            # A trusted join with no new events never re-runs Vision.
            await runtime.apply_changeset_trusted(CHG)
            replay = await events.replay(
                result.changeset.session_id, after_seq=0, limit=100
            )
            assert len(_vision_events(replay)) == 1
            assert len(provider.requests) == 1
        finally:
            await runtime._shutdown()
            await db.close()

    asyncio.run(scenario())


def test_vision_unavailable_without_provider_still_records_truthful_evidence(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db, events, _repo, _changesets, _seed_bridge = await _seed(
            db_path, changeset=_box_changeset()
        )
        bridge = FakeBridgeWithCapture()
        runtime = _service(db, _paths(db_path, tmp_path), bridge)
        try:
            result = await runtime.apply_changeset_trusted(CHG)
            assert result.state is ChangeSetState.APPLIED
            replay = await events.replay(
                result.changeset.session_id, after_seq=0, limit=100
            )
            (vision_event,) = _vision_events(replay)
            payload = vision_event.payload
            assert payload["vision_status"] == "unavailable"
            assert payload["vision_report"] is None
            decision = payload["final_decision"]
            assert decision["status"] == "unavailable"
            # Deterministic validation passed; the unavailable advisory
            # provider cannot turn the delivery into a failure.
            assert decision["deterministic_valid"] is True
            assert decision["accepted"] is True
        finally:
            await runtime._shutdown()
            await db.close()

    asyncio.run(scenario())


def test_vision_cannot_override_deterministic_failure(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db, events, _repo, _changesets, _seed_bridge = await _seed(
            db_path, changeset=_box_changeset()
        )
        bridge = FailingGeometryBridge()
        provider = FakeVisionProvider()
        runtime = _service(db, _paths(db_path, tmp_path), bridge, provider)
        try:
            result = await runtime.apply_changeset_trusted(CHG)
            assert result.state is ChangeSetState.APPLIED
            replay = await events.replay(result.changeset.session_id, after_seq=0, limit=100)
            validation = next(
                event
                for event in replay.events
                if event.event_type == "modeling.validation_completed"
            )
            assert validation.payload["complete"] is False
            # The provider still evaluated the captured bytes...
            assert provider.received == [_CAPTURE_PNG]
            (vision_event,) = _vision_events(replay)
            payload = vision_event.payload
            assert payload["vision_status"] == "completed"
            assert payload["vision_report"]["advisory_passed"] is True
            decision = payload["final_decision"]
            # ...but an advisory pass never overrides deterministic failure.
            assert decision["deterministic_valid"] is False
            assert decision["accepted"] is False
        finally:
            await runtime._shutdown()
            await db.close()

    asyncio.run(scenario())


def test_vision_provider_failure_never_fails_apply(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db, events, _repo, _changesets, _seed_bridge = await _seed(
            db_path, changeset=_box_changeset()
        )
        bridge = FakeBridgeWithCapture()
        provider = FakeVisionProvider()
        provider.response = RuntimeError("provider exploded")
        runtime = _service(db, _paths(db_path, tmp_path), bridge, provider)
        try:
            result = await runtime.apply_changeset_trusted(CHG)
            assert result.state is ChangeSetState.APPLIED
            replay = await events.replay(result.changeset.session_id, after_seq=0, limit=100)
            (vision_event,) = _vision_events(replay)
            payload = vision_event.payload
            assert payload["vision_status"] == "failed"
            assert payload["vision_report"] is None
            decision = payload["final_decision"]
            assert decision["status"] == "failed"
            assert decision["deterministic_valid"] is True
            assert decision["accepted"] is False
            assert decision["summary"] == "Visual evaluation provider failed."
        finally:
            await runtime._shutdown()
            await db.close()

    asyncio.run(scenario())


def test_capture_failure_skips_vision_event(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db, events, _repo, _changesets, _seed_bridge = await _seed(
            db_path, changeset=_box_changeset()
        )
        bridge = FakeBridgeWithCapture()
        bridge.capture_error = RuntimeError("capture exploded")
        provider = FakeVisionProvider()
        runtime = _service(db, _paths(db_path, tmp_path), bridge, provider)
        try:
            result = await runtime.apply_changeset_trusted(CHG)
            assert result.state is ChangeSetState.APPLIED
            replay = await events.replay(result.changeset.session_id, after_seq=0, limit=100)
            assert any(
                event.event_type == "modeling.capture_failed"
                for event in replay.events
            )
            # No registered artifact means no Vision request and no event.
            assert _vision_events(replay) == []
            assert provider.requests == []
            assert provider.received == []
        finally:
            await runtime._shutdown()
            await db.close()

    asyncio.run(scenario())
