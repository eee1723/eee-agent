"""Strict ``workspace.v1`` Bridge inspection contracts.

The DTOs in this module carry bounded, immutable facts only.  They import no
Houdini module and provide no scene mutation surface.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from eee_agent.changesets.contracts import WorkspaceManifest
from eee_agent.core.ids import IdKind, require_id
from eee_agent.houdini_bridge.changesets import _decode_manifest
from eee_agent.houdini_bridge.contracts import (
    MAX_DEADLINE_MS,

    PROTOCOL,
    BridgeError,
    SceneBinding,
    _load_strict_json,
)
from eee_agent.runtime.models import canonical_digest, canonical_json_dumps

WORKSPACE_V1 = "workspace.v1"
WORKSPACE_INSPECT_OPERATION = "workspace.inspect"

_MAX_REQUEST_ID_LEN = 128
_MAX_DEADLINE_MS = MAX_DEADLINE_MS
_MAX_NODE_PATH_LEN = 1024
_MAX_NODE_TYPE_LEN = 256
_MAX_IDENTIFIER_LEN = 128
_MAX_OBSERVATIONS = 4096
_MAX_RESULT_BYTES = 256 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f]")

_REQUEST_FIELDS = frozenset(
    {
        "protocol",
        "kind",
        "request_id",
        "operation",
        "deadline_ms",
        "scene_epoch",
        "payload",
    }
)
_PAYLOAD_FIELDS = frozenset({"mode", "manifest"})
_OBSERVATION_FIELDS = frozenset(
    {
        "path",
        "node_type",
        "parent_path",
        "is_locked",
        "workspace_id",
        "node_id",
        "capability",
        "role",
        "schema_version",
        "created_by_run",
    }
)
_RESULT_FIELDS = frozenset(
    {
        "binding",
        "mode",
        "observations",
        "observed_revision",
        "scene_may_have_changed",
    }
)
_RESPONSE_BASE_FIELDS = frozenset({"protocol", "kind", "request_id", "ok"})


class WorkspaceInspectionUnavailable(Exception):
    """An ordinary missing, closed, timed-out, or incompatible Bridge."""


class WorkspaceInspectionConflict(Exception):
    """A bounded live EEE identity ambiguity."""


class WorkspaceInspectError(Exception):
    """Structured Houdini-side inspection error safe for a Bridge envelope."""

    __slots__ = ("code", "category", "message_for_user", "retryable")

    def __init__(
        self,
        *,
        code: str,
        category: str,
        message_for_user: str,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.category = category
        self.message_for_user = message_for_user
        self.retryable = retryable
        super().__init__(message_for_user)


class WorkspaceInspectLimitError(ValueError):
    """A locally constructed result exceeds its aggregate byte budget."""


def _exact_dict(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact dict")
    return value


def _exact_keys(
    value: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    if set(value.keys()) != expected:
        raise ValueError(f"{label} must contain exactly the required fields")


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be an exact string")
    if not value or len(value) > maximum or _CONTROL_RE.search(value) is not None:
        raise ValueError(f"{label} is not a bounded string")
    return value


def _path(value: object, label: str) -> str:
    text = _bounded_text(value, label, _MAX_NODE_PATH_LEN)
    if not text.startswith("/") or (len(text) > 1 and text.endswith("/")):
        raise ValueError(f"{label} must be an absolute Houdini node path")
    return text


def _identifier_or_none(value: object, label: str) -> str | None:
    if value is None:
        return None
    text = _bounded_text(value, label, _MAX_IDENTIFIER_LEN)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be an identifier")
    return text


def _id_or_none(value: object, kind: IdKind, label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError(f"{label} must be an exact string or None")
    return require_id(value, kind)


def _mode(value: object) -> str:
    if type(value) is not str or value not in ("selection", "manifest"):
        raise ValueError("workspace inspection mode must be selection or manifest")
    return value


def _request_id(value: object) -> str:
    return _bounded_text(value, "request_id", _MAX_REQUEST_ID_LEN)


def _deadline(value: object) -> int:
    if type(value) is not int:
        raise TypeError("deadline_ms must be an exact integer")
    if value < 1 or value > _MAX_DEADLINE_MS:
        raise ValueError("deadline_ms must be in 1..30000")
    return value


def _scene_epoch(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError("scene_epoch must be an exact integer or None")
    if value < 1:
        raise ValueError("scene_epoch must be >= 1")
    return value


def _observation_sort_key(
    value: "WorkspaceNodeObservation",
) -> tuple[bool, str, str]:
    return value.node_id is None, value.node_id or "", value.path


@dataclass(frozen=True, slots=True)
class WorkspaceNodeObservation:
    path: str
    node_type: str
    parent_path: str
    is_locked: bool
    workspace_id: str | None
    node_id: str | None
    capability: str | None
    role: str | None
    schema_version: int | None
    created_by_run: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _path(self.path, "observation.path"))
        object.__setattr__(
            self,
            "node_type",
            _bounded_text(
                self.node_type, "observation.node_type", _MAX_NODE_TYPE_LEN
            ),
        )
        object.__setattr__(
            self, "parent_path", _path(self.parent_path, "observation.parent_path")
        )
        if type(self.is_locked) is not bool:
            raise TypeError("observation.is_locked must be an exact bool")
        object.__setattr__(
            self,
            "workspace_id",
            _id_or_none(self.workspace_id, IdKind.WORKSPACE, "observation.workspace_id"),
        )
        object.__setattr__(
            self, "node_id", _identifier_or_none(self.node_id, "observation.node_id")
        )
        object.__setattr__(
            self,
            "capability",
            _identifier_or_none(self.capability, "observation.capability"),
        )
        object.__setattr__(
            self, "role", _identifier_or_none(self.role, "observation.role")
        )
        if self.schema_version is not None:
            if type(self.schema_version) is not int:
                raise TypeError("observation.schema_version must be an exact int or None")
            if self.schema_version < 1:
                raise ValueError("observation.schema_version must be >= 1")
        object.__setattr__(
            self,
            "created_by_run",
            _id_or_none(
                self.created_by_run, IdKind.RUN, "observation.created_by_run"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "node_type": self.node_type,
            "parent_path": self.parent_path,
            "is_locked": self.is_locked,
            "workspace_id": self.workspace_id,
            "node_id": self.node_id,
            "capability": self.capability,
            "role": self.role,
            "schema_version": self.schema_version,
            "created_by_run": self.created_by_run,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "WorkspaceNodeObservation":
        value = _exact_dict(data, "WorkspaceNodeObservation")
        _exact_keys(value, _OBSERVATION_FIELDS, "WorkspaceNodeObservation")
        return cls(**value)  # type: ignore[arg-type]


def _normalize_observations(
    value: object, *, require_canonical_order: bool
) -> tuple[WorkspaceNodeObservation, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("observations must be a sequence")
    if len(value) > _MAX_OBSERVATIONS:
        raise ValueError("observations exceed the maximum count")
    observations: list[WorkspaceNodeObservation] = []
    for item in value:
        if type(item) is not WorkspaceNodeObservation:
            raise TypeError("observations must contain exact WorkspaceNodeObservation values")
        observations.append(item)
    canonical = sorted(observations, key=_observation_sort_key)
    if require_canonical_order and observations != canonical:
        raise ValueError("observations are not in canonical order")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for observation in canonical:
        if observation.node_id is not None:
            if observation.node_id in seen_ids:
                raise ValueError("observations contain a duplicate node id")
            seen_ids.add(observation.node_id)
        if observation.path in seen_paths:
            raise ValueError("observations contain a duplicate node path")
        seen_paths.add(observation.path)
    return tuple(canonical)


def _result_revision(
    binding: SceneBinding,
    mode: str,
    observations: tuple[WorkspaceNodeObservation, ...],
) -> str:
    payload = {
        "binding": binding.to_dict(),
        "mode": mode,
        "observations": [item.to_dict() for item in observations],
    }
    return canonical_digest(payload)


@dataclass(frozen=True, slots=True)
class WorkspaceInspectRequest:
    request_id: str
    deadline_ms: int
    scene_epoch: int | None
    mode: str
    manifest: WorkspaceManifest | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _request_id(self.request_id))
        object.__setattr__(self, "deadline_ms", _deadline(self.deadline_ms))
        object.__setattr__(self, "scene_epoch", _scene_epoch(self.scene_epoch))
        object.__setattr__(self, "mode", _mode(self.mode))
        if self.mode == "selection":
            if self.manifest is not None:
                raise ValueError("selection mode requires a null manifest")
        elif type(self.manifest) is not WorkspaceManifest:
            raise TypeError("manifest mode requires an exact WorkspaceManifest")

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": self.request_id,
            "operation": WORKSPACE_INSPECT_OPERATION,
            "deadline_ms": self.deadline_ms,
            "scene_epoch": self.scene_epoch,
            "payload": {
                "mode": self.mode,
                "manifest": self.manifest.to_dict() if self.manifest is not None else None,
            },
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "WorkspaceInspectRequest":
        value = _exact_dict(data, "WorkspaceInspectRequest")
        _exact_keys(value, _REQUEST_FIELDS, "WorkspaceInspectRequest")
        if value["protocol"] != PROTOCOL:
            raise ValueError("workspace request protocol is not supported")
        if value["kind"] != "request":
            raise ValueError("workspace request kind must be request")
        if value["operation"] != WORKSPACE_INSPECT_OPERATION:
            raise ValueError("workspace request operation is not supported")
        payload = _exact_dict(value["payload"], "workspace request payload")
        _exact_keys(payload, _PAYLOAD_FIELDS, "workspace request payload")
        raw_manifest = payload["manifest"]
        manifest = None if raw_manifest is None else _decode_manifest(raw_manifest)
        return cls(
            request_id=value["request_id"],  # type: ignore[arg-type]
            deadline_ms=value["deadline_ms"],  # type: ignore[arg-type]
            scene_epoch=value["scene_epoch"],  # type: ignore[arg-type]
            mode=payload["mode"],  # type: ignore[arg-type]
            manifest=manifest,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceInspectResult:
    binding: SceneBinding
    mode: str
    observations: tuple[WorkspaceNodeObservation, ...]
    observed_revision: str
    scene_may_have_changed: bool = False

    def __post_init__(self) -> None:
        if type(self.binding) is not SceneBinding:
            raise TypeError("binding must be an exact SceneBinding")
        object.__setattr__(self, "mode", _mode(self.mode))
        observations = _normalize_observations(
            self.observations, require_canonical_order=True
        )
        object.__setattr__(self, "observations", observations)
        if type(self.observed_revision) is not str or _SHA256_RE.fullmatch(
            self.observed_revision
        ) is None:
            raise ValueError("observed_revision must be a lowercase SHA-256")
        expected = _result_revision(self.binding, self.mode, observations)
        if self.observed_revision != expected:
            raise ValueError("observed_revision does not match the live fact hash")
        if type(self.scene_may_have_changed) is not bool:
            raise TypeError("scene_may_have_changed must be an exact bool")
        if self.scene_may_have_changed:
            raise ValueError("workspace inspection cannot return uncertain scene facts")
        if len(canonical_json_dumps(self.to_dict()).encode("utf-8")) > _MAX_RESULT_BYTES:
            raise WorkspaceInspectLimitError(
                "workspace inspection exceeds the maximum result size"
            )

    @classmethod
    def build(
        cls,
        *,
        binding: SceneBinding,
        mode: str,
        observations: Sequence[WorkspaceNodeObservation],
    ) -> "WorkspaceInspectResult":
        checked_mode = _mode(mode)
        normalized = _normalize_observations(
            observations, require_canonical_order=False
        )
        return cls(
            binding=binding,
            mode=checked_mode,
            observations=normalized,
            observed_revision=_result_revision(binding, checked_mode, normalized),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "binding": self.binding.to_dict(),
            "mode": self.mode,
            "observations": [item.to_dict() for item in self.observations],
            "observed_revision": self.observed_revision,
            "scene_may_have_changed": self.scene_may_have_changed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "WorkspaceInspectResult":
        value = _exact_dict(data, "WorkspaceInspectResult")
        _exact_keys(value, _RESULT_FIELDS, "WorkspaceInspectResult")
        raw_observations = value["observations"]
        if type(raw_observations) is not list:
            raise TypeError("observations must be an exact list on the wire")
        observations = tuple(
            WorkspaceNodeObservation.from_dict(item)  # type: ignore[arg-type]
            for item in raw_observations
        )
        return cls(
            binding=SceneBinding.from_dict(value["binding"]),  # type: ignore[arg-type]
            mode=value["mode"],  # type: ignore[arg-type]
            observations=observations,
            observed_revision=value["observed_revision"],  # type: ignore[arg-type]
            scene_may_have_changed=value["scene_may_have_changed"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class WorkspaceInspectResponse:
    request_id: str
    result: WorkspaceInspectResult | None
    error: BridgeError | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _request_id(self.request_id))
        if (self.result is None) == (self.error is None):
            raise ValueError("workspace response requires exactly one result or error")
        if self.result is not None and type(self.result) is not WorkspaceInspectResult:
            raise TypeError("workspace response result has the wrong type")
        if self.error is not None and type(self.error) is not BridgeError:
            raise TypeError("workspace response error has the wrong type")

    def to_dict(self) -> dict[str, object]:
        base: dict[str, object] = {
            "protocol": PROTOCOL,
            "kind": "response",
            "request_id": self.request_id,
            "ok": self.error is None,
        }
        if self.result is not None:
            base["result"] = self.result.to_dict()
        else:
            base["error"] = self.error.to_dict()  # type: ignore[union-attr]
        return base

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "WorkspaceInspectResponse":
        value = _exact_dict(data, "WorkspaceInspectResponse")
        if value.get("protocol") != PROTOCOL:
            raise ValueError("workspace response protocol is not supported")
        if value.get("kind") != "response":
            raise ValueError("workspace response kind must be response")
        if type(value.get("ok")) is not bool:
            raise TypeError("workspace response ok must be an exact bool")
        if value["ok"]:
            _exact_keys(
                value,
                _RESPONSE_BASE_FIELDS | {"result"},
                "successful workspace response",
            )
            return cls(
                request_id=value["request_id"],  # type: ignore[arg-type]
                result=WorkspaceInspectResult.from_dict(value["result"]),  # type: ignore[arg-type]
                error=None,
            )
        _exact_keys(
            value,
            _RESPONSE_BASE_FIELDS | {"error"},
            "failed workspace response",
        )
        return cls(
            request_id=value["request_id"],  # type: ignore[arg-type]
            result=None,
            error=BridgeError.from_dict(value["error"]),  # type: ignore[arg-type]
        )


def parse_workspace_inspect_request(raw: object) -> WorkspaceInspectRequest:
    return WorkspaceInspectRequest.from_dict(
        _exact_dict(_load_strict_json(raw, "workspace request"), "workspace request")
    )


def parse_workspace_inspect_response(raw: object) -> WorkspaceInspectResponse:
    return WorkspaceInspectResponse.from_dict(
        _exact_dict(_load_strict_json(raw, "workspace response"), "workspace response")
    )


__all__ = [
    "WORKSPACE_INSPECT_OPERATION",
    "WORKSPACE_V1",
    "WorkspaceInspectRequest",
    "WorkspaceInspectError",
    "WorkspaceInspectLimitError",
    "WorkspaceInspectResponse",
    "WorkspaceInspectResult",
    "WorkspaceInspectionConflict",
    "WorkspaceInspectionUnavailable",
    "WorkspaceNodeObservation",
    "parse_workspace_inspect_request",
    "parse_workspace_inspect_response",
]
