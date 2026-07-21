"""Additive ``sensitivity.v1`` Bridge capability and typed sampling DTOs.

This module adds the smallest auditable parameter sample-and-restore surface
on top of the accepted :mod:`eee_agent.houdini_bridge.changesets` typed
operation pattern (Task 18-F). It defines:

* the advertised :data:`SENSITIVITY_V1` capability;
* frozen, slotted, JSON-canonical DTOs for the ``sensitivity.sample`` request
  and its bounded baseline/samples/restored evidence response; and
* strict parsers (:func:`parse_sample_request` / :func:`parse_sample_response`).

It imports **neither** ``hou`` **nor** ``rpyc``. The operation is an internal
trusted Runtime-to-Bridge operation exactly like ``changeset.apply``: the
write phase runs only on the single main-thread FIFO, every sampled parameter
is restored to its exact original value with read-back verification, and any
uncertainty (stale scene, cook failure, interruption, or an unverifiable
restore) fails closed with a structured error instead of a guessed success.
The request carries only absolute node paths, stable node ids, parameter
names, and literal numeric sample values — never an expression, source, or
callable.

Strictness mirrors the rest of the bridge package: exact primitive types,
exact field sets, deep-frozen canonical JSON, duplicate-key rejection, finite
numbers, and an explicit size limit.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from eee_agent.houdini_bridge.changesets import (
    _MAX_DEADLINE_MS,
    _MAX_NODE_PATH_LEN,
    _MAX_RESULT_BYTES,
    _MIN_DEADLINE_MS,
    _REQUEST_FIELDS,
    _RESPONSE_REQUIRED_FIELDS,
    _load_strict_json,
    _require_exact_bool,
    _require_exact_dict,
    _require_exact_int,
    _require_exact_keys,
    _require_identifier,
    _require_optional_identifier,
    _require_request_id,
)
from eee_agent.houdini_bridge.contracts import (
    PROTOCOL,
    BridgeError,
    SceneQueryResult,
)
from eee_agent.runtime.models import canonical_json_dumps

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

SENSITIVITY_V1 = "sensitivity.v1"
SAMPLE_OPERATION = "sensitivity.sample"

_MAX_SAMPLES = 16
_MAX_NODE_PATHS = 64

# exact field sets for envelope + payload validation
_PAYLOAD_FIELDS = frozenset({"node_paths", "samples"})
_SAMPLE_FIELDS = frozenset({"node_id", "path", "parm_name", "value"})
_RESULT_FIELDS = frozenset({"baseline", "samples", "restored"})


# --------------------------------------------------------------------------
# sample target + request DTOs
# --------------------------------------------------------------------------


def _require_sample_value(value: object, label: str) -> None:
    # Only literal numeric sample values cross the wire; a bool is an exact
    # int subclass and is rejected explicitly (mirrors catalog strictness).
    if type(value) is bool or type(value) not in (int, float):
        raise TypeError(f"{label} must be an exact int or float")
    if type(value) is float and not math.isfinite(value):
        raise ValueError(f"{label} must be finite")


def _require_node_path(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value.startswith("/"):
        raise ValueError(f"{label} must be an absolute Houdini path")
    if len(value) > _MAX_NODE_PATH_LEN:
        raise ValueError(f"{label} exceeds the maximum node path length")


@dataclass(frozen=True, slots=True)
class SensitivitySampleTarget:
    """One bounded sample write: which parameter to perturb and with what.

    ``node_id`` is the stable mirrored id when the Runtime knows it (resolved
    before the path on the Houdini side); ``value`` is the exact literal
    numeric value written for the sample. The original scene value is never
    sent: it is read on the Houdini main thread immediately before the write
    and restored exactly.
    """

    node_id: str | None
    path: str
    parm_name: str
    value: object

    def __post_init__(self) -> None:
        _require_optional_identifier(self.node_id, "SensitivitySampleTarget.node_id")
        _require_node_path(self.path, "SensitivitySampleTarget.path")
        _require_identifier(self.parm_name, "SensitivitySampleTarget.parm_name")
        _require_sample_value(self.value, "SensitivitySampleTarget.value")

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "path": self.path,
            "parm_name": self.parm_name,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SensitivitySampleTarget:
        value = _require_exact_dict(data, "SensitivitySampleTarget")
        _require_exact_keys(value, _SAMPLE_FIELDS, "SensitivitySampleTarget")
        return cls(
            node_id=value["node_id"],  # type: ignore[arg-type]
            path=value["path"],  # type: ignore[arg-type]
            parm_name=value["parm_name"],  # type: ignore[arg-type]
            value=value["value"],
        )


@dataclass(frozen=True, slots=True)
class SensitivitySampleRequest:
    """A parsed, validated ``sensitivity.sample`` request envelope.

    Carries the bounded evidence node set (the exact compiled node paths the
    baseline/sample/restored scene queries cover) and 1..16 typed sample
    targets. The envelope ``scene_epoch`` is re-checked against the tracked
    scene before any write; a mismatch fails closed with zero writes.
    """

    request_id: str
    deadline_ms: int
    scene_epoch: int
    node_paths: tuple[str, ...]
    samples: tuple[SensitivitySampleTarget, ...]

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "SensitivitySampleRequest.request_id")
        _require_exact_int(self.deadline_ms, "SensitivitySampleRequest.deadline_ms")
        if self.deadline_ms < _MIN_DEADLINE_MS or self.deadline_ms > _MAX_DEADLINE_MS:
            raise ValueError("SensitivitySampleRequest.deadline_ms must be in 1..30000")
        _require_exact_int(self.scene_epoch, "SensitivitySampleRequest.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("SensitivitySampleRequest.scene_epoch must be >= 1")
        if isinstance(self.node_paths, str) or not isinstance(
            self.node_paths, Sequence
        ):
            raise TypeError("SensitivitySampleRequest.node_paths must be a sequence")
        node_paths = tuple(self.node_paths)
        if not node_paths or len(node_paths) > _MAX_NODE_PATHS:
            raise ValueError("SensitivitySampleRequest.node_paths must contain 1..64 paths")
        if len(set(node_paths)) != len(node_paths):
            raise ValueError("SensitivitySampleRequest.node_paths contains duplicates")
        for path in node_paths:
            _require_node_path(path, "SensitivitySampleRequest.node_paths")
        object.__setattr__(self, "node_paths", node_paths)
        if isinstance(self.samples, str) or not isinstance(self.samples, Sequence):
            raise TypeError("SensitivitySampleRequest.samples must be a sequence")
        samples = tuple(self.samples)
        if not samples or len(samples) > _MAX_SAMPLES:
            raise ValueError("SensitivitySampleRequest.samples must contain 1..16 targets")
        if any(type(item) is not SensitivitySampleTarget for item in samples):
            raise TypeError(
                "SensitivitySampleRequest.samples must contain SensitivitySampleTarget"
            )
        object.__setattr__(self, "samples", samples)

    @classmethod
    def build(
        cls,
        *,
        request_id: str,
        deadline_ms: int,
        scene_epoch: int,
        node_paths: Sequence[str],
        samples: Sequence[SensitivitySampleTarget],
    ) -> SensitivitySampleRequest:
        return cls(
            request_id=request_id,
            deadline_ms=deadline_ms,
            scene_epoch=scene_epoch,
            node_paths=tuple(node_paths),
            samples=tuple(samples),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": self.request_id,
            "operation": SAMPLE_OPERATION,
            "deadline_ms": self.deadline_ms,
            "scene_epoch": self.scene_epoch,
            "payload": {
                "node_paths": list(self.node_paths),
                "samples": [item.to_dict() for item in self.samples],
            },
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SensitivitySampleRequest:
        envelope = _require_exact_dict(data, "SensitivitySampleRequest envelope")
        _require_exact_keys(envelope, _REQUEST_FIELDS, "SensitivitySampleRequest envelope")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("SensitivitySampleRequest protocol must be eee.bridge/1")
        if envelope["kind"] != "request":
            raise ValueError("SensitivitySampleRequest kind must be request")
        if envelope["operation"] != SAMPLE_OPERATION:
            raise ValueError("SensitivitySampleRequest operation must be sensitivity.sample")
        payload = _require_exact_dict(envelope["payload"], "SensitivitySampleRequest payload")
        _require_exact_keys(payload, _PAYLOAD_FIELDS, "SensitivitySampleRequest payload")
        node_paths = payload["node_paths"]
        if type(node_paths) is not list:
            raise TypeError("SensitivitySampleRequest payload node_paths must be a list")
        samples = payload["samples"]
        if type(samples) is not list:
            raise TypeError("SensitivitySampleRequest payload samples must be a list")
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            deadline_ms=envelope["deadline_ms"],  # type: ignore[arg-type]
            scene_epoch=envelope["scene_epoch"],  # type: ignore[arg-type]
            node_paths=tuple(node_paths),  # type: ignore[arg-type]
            samples=tuple(SensitivitySampleTarget.from_dict(item) for item in samples),  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# result + response DTOs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SensitivitySampleResult:
    """The bounded evidence of one completed sample-and-restore cycle.

    A result is returned only when every sample was captured and every written
    parameter was restored to its exact original value with read-back
    verification; anything less surfaces as a structured bridge error, never
    as a partial result. ``baseline``, each of ``samples``, and ``restored``
    are captured over the same node set at the same scene epoch.
    """

    baseline: SceneQueryResult
    samples: tuple[SceneQueryResult, ...]
    restored: SceneQueryResult

    def __post_init__(self) -> None:
        if type(self.baseline) is not SceneQueryResult:
            raise TypeError("SensitivitySampleResult.baseline must be a SceneQueryResult")
        if type(self.restored) is not SceneQueryResult:
            raise TypeError("SensitivitySampleResult.restored must be a SceneQueryResult")
        if isinstance(self.samples, str) or not isinstance(self.samples, Sequence):
            raise TypeError("SensitivitySampleResult.samples must be a sequence")
        samples = tuple(self.samples)
        if not samples or len(samples) > _MAX_SAMPLES:
            raise ValueError("SensitivitySampleResult.samples must contain 1..16 results")
        if any(type(item) is not SceneQueryResult for item in samples):
            raise TypeError(
                "SensitivitySampleResult.samples must contain SceneQueryResult values"
            )
        object.__setattr__(self, "samples", samples)
        if len(canonical_json_dumps(self.to_dict()).encode("utf-8")) > _MAX_RESULT_BYTES:
            raise ValueError("SensitivitySampleResult exceeds the maximum result size")

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline": self.baseline.to_dict(),
            "samples": [item.to_dict() for item in self.samples],
            "restored": self.restored.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SensitivitySampleResult:
        value = _require_exact_dict(data, "SensitivitySampleResult")
        _require_exact_keys(value, _RESULT_FIELDS, "SensitivitySampleResult")
        samples = value["samples"]
        if type(samples) is not list:
            raise TypeError("SensitivitySampleResult.samples must be a list")
        return cls(
            baseline=SceneQueryResult.from_dict(value["baseline"]),  # type: ignore[arg-type]
            samples=tuple(SceneQueryResult.from_dict(item) for item in samples),
            restored=SceneQueryResult.from_dict(value["restored"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class SensitivitySampleResponse:
    """A parsed, validated ``sensitivity.sample`` response envelope.

    Carries exactly one of a typed :class:`SensitivitySampleResult` or a
    :class:`BridgeError`. A malformed evidence payload (bad binding, duplicate
    keys, oversized result) fails closed at parse time.
    """

    request_id: str
    result: SensitivitySampleResult | None
    error: BridgeError | None

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "SensitivitySampleResponse.request_id")
        if self.result is not None and type(self.result) is not SensitivitySampleResult:
            raise TypeError(
                "SensitivitySampleResponse.result must be a SensitivitySampleResult or None"
            )
        if self.error is not None and type(self.error) is not BridgeError:
            raise TypeError("SensitivitySampleResponse.error must be an exact BridgeError or None")
        if (self.result is None) == (self.error is None):
            raise ValueError("SensitivitySampleResponse must carry exactly one of result or error")

    def to_dict(self) -> dict[str, object]:
        if self.result is not None:
            return {
                "protocol": PROTOCOL,
                "kind": "response",
                "request_id": self.request_id,
                "ok": True,
                "result": self.result.to_dict(),
            }
        return {
            "protocol": PROTOCOL,
            "kind": "response",
            "request_id": self.request_id,
            "ok": False,
            "error": self.error.to_dict(),  # type: ignore[union-attr]
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SensitivitySampleResponse:
        envelope = _require_exact_dict(data, "SensitivitySampleResponse envelope")
        if not _RESPONSE_REQUIRED_FIELDS.issubset(envelope.keys()):
            raise ValueError("SensitivitySampleResponse envelope is missing required fields")
        extra = set(envelope.keys()) - _RESPONSE_REQUIRED_FIELDS - {"result", "error"}
        if extra:
            raise ValueError("SensitivitySampleResponse envelope has unknown fields")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("SensitivitySampleResponse protocol must be eee.bridge/1")
        if envelope["kind"] != "response":
            raise ValueError("SensitivitySampleResponse kind must be response")
        ok = envelope["ok"]
        _require_exact_bool(ok, "SensitivitySampleResponse.ok")
        if ok is True:
            result = envelope.get("result")
            error = envelope.get("error")
            if result is None or error is not None:
                raise ValueError(
                    "SensitivitySampleResponse ok=true requires result and no error"
                )
            return cls(
                request_id=envelope["request_id"],  # type: ignore[arg-type]
                result=SensitivitySampleResult.from_dict(result),  # type: ignore[arg-type]
                error=None,
            )
        error = envelope.get("error")
        result = envelope.get("result")
        if error is None or result is not None:
            raise ValueError(
                "SensitivitySampleResponse ok=false requires error and no result"
            )
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            result=None,
            error=BridgeError.from_dict(error),  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# JSON text entrypoints
# --------------------------------------------------------------------------


def parse_sample_request(raw: str | bytes) -> SensitivitySampleRequest:
    """Parse a ``sensitivity.sample`` request from strict JSON text."""
    return SensitivitySampleRequest.from_dict(_load_strict_json(raw, "Sample request"))


def parse_sample_response(raw: str | bytes) -> SensitivitySampleResponse:
    """Parse a ``sensitivity.sample`` response from strict JSON text."""
    return SensitivitySampleResponse.from_dict(_load_strict_json(raw, "Sample response"))
