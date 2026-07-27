"""Bounded modeling-brief compiler for the HTML sketch workflow.

The model may interpret a user's natural-language request, but the result must
cross this strict seam before a procedural HTML sketch can be rendered.  The
returned digest binds the approved brief to ``render_sketch`` and gives the
later Houdini translation a stable source of dimensions, axes, components,
constraints, and acceptance criteria.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Literal

from langchain_core.tools import tool
from typing_extensions import TypedDict


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_AXES = frozenset({"+X", "-X", "+Y", "-Y", "+Z", "-Z"})
_AXIS_VECTORS = {
    "+X": (1, 0, 0),
    "-X": (-1, 0, 0),
    "+Y": (0, 1, 0),
    "-Y": (0, -1, 0),
    "+Z": (0, 0, 1),
    "-Z": (0, 0, -1),
}
_VECTOR_AXES = {value: key for key, value in _AXIS_VECTORS.items()}
_DETAIL_LEVELS = frozenset({"blockout", "standard", "high"})
_UNITS = frozenset({"m", "cm", "mm"})
_VIEWS = frozenset(
    {"three_quarter", "side", "front", "top", "rear", "detail"}
)
_BRIEF_KEYS = frozenset(
    {
        "schema_version",
        "brief_key",
        "title",
        "asset_family",
        "original_request",
        "units",
        "up_axis",
        "forward_axis",
        "detail_level",
        "components",
        "dimensions",
        "orientation_rules",
        "constraints",
        "acceptance",
        "views",
        "assumptions",
        "clarification_questions",
    }
)
_COMPONENT_KEYS = frozenset({"name", "count", "required", "details"})
_MAX_COMPONENTS = 32
_MAX_COMPONENT_DETAILS = 16
_MAX_DIMENSIONS = 32
_MAX_RULES = 32
_MAX_VIEWS = 6
_MAX_ASSUMPTIONS = 16
_MAX_QUESTIONS = 3
_MAX_TEXT = 512
_MAX_REQUEST = 4096


class ModelingBriefComponentInput(TypedDict):
    name: str
    count: int
    required: bool
    details: list[str]


class ModelingBriefInput(TypedDict):
    schema_version: Literal[1]
    brief_key: str
    title: str
    asset_family: str
    original_request: str
    units: Literal["m", "cm", "mm"]
    up_axis: Literal["+X", "-X", "+Y", "-Y", "+Z", "-Z"]
    forward_axis: Literal["+X", "-X", "+Y", "-Y", "+Z", "-Z"]
    detail_level: Literal["blockout", "standard", "high"]
    components: list[ModelingBriefComponentInput]
    dimensions: dict[str, float]
    orientation_rules: list[str]
    constraints: list[str]
    acceptance: list[str]
    views: list[
        Literal["three_quarter", "side", "front", "top", "rear", "detail"]
    ]
    assumptions: list[str]
    clarification_questions: list[str]


class ModelingBriefInputError(ValueError):
    """A bounded validation error safe to map to a public tool result."""


def _error(message: str) -> dict[str, object]:
    return {
        "ok": False,
        "code": "workflow.modeling_brief_invalid",
        "message": message,
    }


def _exact_mapping(
    value: object, *, keys: frozenset[str], label: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ModelingBriefInputError(
            f"{label} must contain exactly: {', '.join(sorted(keys))}."
        )
    return value


def _text(value: object, *, label: str, limit: int = _MAX_TEXT) -> str:
    if type(value) is not str:
        raise ModelingBriefInputError(f"{label} must be text.")
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > limit:
        raise ModelingBriefInputError(
            f"{label} must contain 1..{limit} characters."
        )
    return cleaned


def _identifier(value: object, *, label: str) -> str:
    cleaned = _text(value, label=label, limit=64)
    if _IDENTIFIER_RE.fullmatch(cleaned) is None:
        raise ModelingBriefInputError(f"{label} must be an identifier.")
    return cleaned


def _sequence(
    value: object, *, label: str, maximum: int
) -> Sequence[object]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) > maximum
    ):
        raise ModelingBriefInputError(
            f"{label} must be a list with at most {maximum} entries."
        )
    return value


def _unique_text_list(
    value: object,
    *,
    label: str,
    maximum: int,
    allowed: frozenset[str] | None = None,
) -> list[str]:
    items = _sequence(value, label=label, maximum=maximum)
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        cleaned = _text(item, label=f"{label}[{index}]")
        if allowed is not None and cleaned not in allowed:
            raise ModelingBriefInputError(
                f"{label}[{index}] is not an allowed value."
            )
        folded = cleaned.casefold()
        if folded in seen:
            raise ModelingBriefInputError(f"{label} contains a duplicate.")
        seen.add(folded)
        result.append(cleaned)
    return result


def _components(value: object) -> list[dict[str, object]]:
    items = _sequence(value, label="brief.components", maximum=_MAX_COMPONENTS)
    result: list[dict[str, object]] = []
    names: set[str] = set()
    for index, item in enumerate(items):
        data = _exact_mapping(
            item, keys=_COMPONENT_KEYS, label=f"brief.components[{index}]"
        )
        name = _identifier(
            data["name"], label=f"brief.components[{index}].name"
        )
        if name in names:
            raise ModelingBriefInputError("brief.components contains a duplicate name.")
        names.add(name)
        count = data["count"]
        if type(count) is not int or not 1 <= count <= 1024:
            raise ModelingBriefInputError(
                f"brief.components[{index}].count must be an integer in 1..1024."
            )
        required = data["required"]
        if type(required) is not bool:
            raise ModelingBriefInputError(
                f"brief.components[{index}].required must be boolean."
            )
        details = _unique_text_list(
            data["details"],
            label=f"brief.components[{index}].details",
            maximum=_MAX_COMPONENT_DETAILS,
        )
        result.append(
            {
                "name": name,
                "count": count,
                "required": required,
                "details": details,
            }
        )
    return result


def _dimensions(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping) or len(value) > _MAX_DIMENSIONS:
        raise ModelingBriefInputError(
            f"brief.dimensions must be an object with at most {_MAX_DIMENSIONS} entries."
        )
    result: dict[str, float] = {}
    for raw_name, raw_value in value.items():
        name = _identifier(raw_name, label="brief.dimensions key")
        if type(raw_value) not in (int, float):
            raise ModelingBriefInputError(
                f"brief.dimensions.{name} must be a finite positive number."
            )
        number = float(raw_value)
        if not math.isfinite(number) or number <= 0:
            raise ModelingBriefInputError(
                f"brief.dimensions.{name} must be a finite positive number."
            )
        result[name] = number
    return dict(sorted(result.items()))


def _lateral_axis(forward_axis: str, up_axis: str) -> str:
    forward = _AXIS_VECTORS[forward_axis]
    up = _AXIS_VECTORS[up_axis]
    dot = sum(a * b for a, b in zip(forward, up, strict=True))
    if dot != 0:
        raise ModelingBriefInputError(
            "brief.forward_axis must be perpendicular to brief.up_axis."
        )
    # The project names the positive lateral direction "left": forward × up.
    lateral = (
        forward[1] * up[2] - forward[2] * up[1],
        forward[2] * up[0] - forward[0] * up[2],
        forward[0] * up[1] - forward[1] * up[0],
    )
    return _VECTOR_AXES[lateral]


def normalize_modeling_brief(value: object) -> tuple[dict[str, object], list[str]]:
    """Validate and canonicalize one modeling-brief tool payload."""
    data = _exact_mapping(value, keys=_BRIEF_KEYS, label="brief")
    if data["schema_version"] != 1 or type(data["schema_version"]) is not int:
        raise ModelingBriefInputError("brief.schema_version must be 1.")

    units = data["units"]
    if type(units) is not str or units not in _UNITS:
        raise ModelingBriefInputError("brief.units must be m, cm, or mm.")
    up_axis = data["up_axis"]
    forward_axis = data["forward_axis"]
    if type(up_axis) is not str or up_axis not in _AXES:
        raise ModelingBriefInputError("brief.up_axis must be a signed world axis.")
    if type(forward_axis) is not str or forward_axis not in _AXES:
        raise ModelingBriefInputError(
            "brief.forward_axis must be a signed world axis."
        )
    left_axis = _lateral_axis(forward_axis, up_axis)

    detail_level = data["detail_level"]
    if type(detail_level) is not str or detail_level not in _DETAIL_LEVELS:
        raise ModelingBriefInputError(
            "brief.detail_level must be blockout, standard, or high."
        )

    questions = _unique_text_list(
        data["clarification_questions"],
        label="brief.clarification_questions",
        maximum=_MAX_QUESTIONS,
    )
    components = _components(data["components"])
    orientation_rules = _unique_text_list(
        data["orientation_rules"],
        label="brief.orientation_rules",
        maximum=_MAX_RULES,
    )
    constraints = _unique_text_list(
        data["constraints"], label="brief.constraints", maximum=_MAX_RULES
    )
    acceptance = _unique_text_list(
        data["acceptance"], label="brief.acceptance", maximum=_MAX_RULES
    )
    views = _unique_text_list(
        data["views"],
        label="brief.views",
        maximum=_MAX_VIEWS,
        allowed=_VIEWS,
    )
    assumptions = _unique_text_list(
        data["assumptions"],
        label="brief.assumptions",
        maximum=_MAX_ASSUMPTIONS,
    )

    if not questions:
        if not components:
            raise ModelingBriefInputError(
                "brief.components must not be empty when the brief is ready."
            )
        if not orientation_rules:
            raise ModelingBriefInputError(
                "brief.orientation_rules must not be empty when the brief is ready."
            )
        if not constraints:
            raise ModelingBriefInputError(
                "brief.constraints must not be empty when the brief is ready."
            )
        if not acceptance:
            raise ModelingBriefInputError(
                "brief.acceptance must not be empty when the brief is ready."
            )
        if not views:
            raise ModelingBriefInputError(
                "brief.views must not be empty when the brief is ready."
            )

    normalized: dict[str, object] = {
        "schema_version": 1,
        "brief_key": _identifier(data["brief_key"], label="brief.brief_key"),
        "title": _text(data["title"], label="brief.title"),
        "asset_family": _identifier(
            data["asset_family"], label="brief.asset_family"
        ),
        "original_request": _text(
            data["original_request"],
            label="brief.original_request",
            limit=_MAX_REQUEST,
        ),
        "units": units,
        "coordinate_frame": {
            "up_axis": up_axis,
            "forward_axis": forward_axis,
            "left_axis": left_axis,
        },
        "detail_level": detail_level,
        "components": components,
        "dimensions": _dimensions(data["dimensions"]),
        "orientation_rules": orientation_rules,
        "constraints": constraints,
        "acceptance": acceptance,
        "views": views,
        "assumptions": assumptions,
    }
    return normalized, questions


@tool
async def prepare_modeling_brief(brief: ModelingBriefInput) -> dict[str, object]:
    """Compile a user's design request into a bounded modeling contract.

    Call this before ``render_sketch`` for every procedural/parametric or
    complex design asset.  ``brief`` must contain exactly these keys:

    schema_version=1; brief_key/title/asset_family/original_request; units
    ("m"|"cm"|"mm"); up_axis and forward_axis (signed axes such as "+Y" and
    "+X", perpendicular); detail_level ("blockout"|"standard"|"high");
    components=[{name identifier,count 1..1024,required bool,details [text]}];
    dimensions={identifier: positive number}; orientation_rules, constraints,
    acceptance and assumptions as text lists; views chosen from
    three_quarter/side/front/top/rear/detail; clarification_questions as ONE
    batch of zero to three questions.

    Ask questions only when component scope or detail level would materially
    change the asset.  Put all questions in one call (maximum three), show the
    returned questions to the user, and stop.  After the user answers, call
    again with an empty clarification_questions list and reasonable defaults
    for anything still unspecified.  A ready result returns ``brief_digest``;
    pass that exact digest to ``render_sketch``.
    """
    try:
        normalized, questions = normalize_modeling_brief(brief)
    except ModelingBriefInputError as exc:
        return _error(str(exc))
    if questions:
        return {
            "ok": True,
            "ready": False,
            "question_count": len(questions),
            "questions": questions,
            "draft": normalized,
        }
    canonical = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "ok": True,
        "ready": True,
        "brief_digest": digest,
        "brief": normalized,
    }


__all__ = [
    "ModelingBriefInputError",
    "ModelingBriefComponentInput",
    "ModelingBriefInput",
    "normalize_modeling_brief",
    "prepare_modeling_brief",
]
