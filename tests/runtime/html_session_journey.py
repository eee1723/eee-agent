"""L4 acceptance: two-round HTML-sketch Agent session against real Houdini.

This journey extends the single-round provider acceptance
(:mod:`provider_journey`, which it imports and reuses for the worker
lifecycle, wiring, replay, and bounded-evidence mechanics) to the full
HTML-sketch pipeline contract:

Run A (user request): the agent must analyze the component list and
real-world dimensions, author ONE self-contained Three.js HTML sketch, call
``render_sketch`` once, report the PNG path, and STOP for review — with ZERO
Houdini write tool calls (``scratch_build`` / ``scratch_commit`` /
``cleanup_nodes`` tool.started counts must all be 0; this is the core
acceptance point).

Run B (user approval, same session): the agent must reuse the approved
sketch and dimension intent, build with ``scratch_build`` (catalog nodes,
literal parameters, semantic part names), pass ``verify_geometry``
(ok=true, empty issues) and only then ``scratch_commit`` to
``/obj/eee_html_<case>`` (skip_structure_check=true, no orientation checks).

Afterwards the committed path and its geometry envelope are read back
through the Secure Bridge, the run sandbox must be gone, the durable event
log must replay both rounds' required tool events after a service restart,
and the hython worker performs scene cleanup. A strict bounded evidence
record is written to ``EEE_RUNTIME_MVP_EVIDENCE_PATH``.

Usage (PowerShell)::

    $env:EEE_RUNTIME_HOME = "..."            # disposable runtime home
    $env:EEE_RUNTIME_MVP_HFS = "...\\bin\\hython.exe"
    $env:EEE_RUNTIME_MVP_HIP_PATH = "...\\scene.hip"
    $env:EEE_RUNTIME_MVP_EVIDENCE_PATH = "...\\evidence.json"
    python tests/runtime/html_session_journey.py --case chair

Honest CI semantics: without ``--case``, without provider credentials, or
without a usable hython/HFS, the script prints a bounded ``not_run``/usage
record and exits 0. Only genuine journey failures exit 1.

Credential hygiene matches provider_journey: provider output, prompts, and
secrets are never printed and never written into the evidence record; the
hython worker gets a scrubbed environment and logs into the runtime home.

The module intentionally imports only stdlib (plus the stdlib-only
``provider_journey`` module) at module scope so the environment/credential
validation runs before ``eee_agent.config`` (which loads ``.env``) can
influence the process environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# provider_journey is import-safe (its main runs only under __main__) and
# stdlib-only at module scope. The fallback keeps the import working when
# this file is imported as a package member instead of run as a script.
try:
    import provider_journey as _pj
except ImportError:  # pragma: no cover - package-style import fallback
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import provider_journey as _pj

_StepError = _pj._StepError

_RUN_TIMEOUT_SECONDS = 900.0

_EVIDENCE_FIELDS = frozenset(
    {
        "case",
        "run_a_status",
        "render_sketch_ok",
        "png_ok",
        "pre_approval_houdini_writes",
        "run_b_status",
        "scratch_build_ok",
        "geometry_verified",
        "commit_committed",
        "final_path",
        "final_geometry_ok",
        "final_bbox",
        "sandbox_absent",
        "restart_replay_last_seq",
        "scene_cleanup",
    }
)
_MAX_EVIDENCE_BYTES = 16 * 1024

_SKETCH_EVIDENCE_FIELDS = frozenset(
    {
        "case",
        "run_a_status",
        "render_sketch_ok",
        "png_ok",
        "pre_approval_houdini_writes",
        "sketch_html_ok",
        "scene_cleanup",
    }
)

# Run A must not start ANY Houdini write tool before the user approves.
_PRE_APPROVAL_FORBIDDEN_TOOLS = ("scratch_build", "scratch_commit", "cleanup_nodes")
_RUN_B_REQUIRED_TOOLS = ("scratch_build", "verify_geometry", "scratch_commit")

_NODE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class _Envelope:
    """Loose committed-geometry envelope for one case (meters, points)."""

    size_x: tuple[float, float]
    size_y: tuple[float, float]
    size_z: tuple[float, float]
    bbox_min_y: tuple[float, float]
    min_points: int


@dataclass(frozen=True, slots=True)
class _CaseSpec:
    label: str
    target_name: str
    dimensions: str
    parts: str
    envelope: _Envelope


_CASES: dict[str, _CaseSpec] = {
    "chair": _CaseSpec(
        label="dining chair",
        target_name="eee_html_chair",
        dimensions=(
            "seat panel 0.45m x 0.45m with its top surface 0.45m above the "
            "floor; four legs; backrest rising 0.45m above the seat; total "
            "height about 0.9m"
        ),
        parts="seat, leg_fl, leg_fr, leg_bl, leg_br, backrest",
        envelope=_Envelope(
            size_x=(0.3, 0.8),
            size_y=(0.6, 1.3),
            size_z=(0.3, 0.8),
            bbox_min_y=(-0.05, 0.1),
            min_points=24,
        ),
    ),
    "desk": _CaseSpec(
        label="writing desk",
        target_name="eee_html_desk",
        dimensions=(
            "tabletop 1.2m x 0.6m, 0.04m thick, with its top surface 0.72m "
            "above the floor; four legs"
        ),
        parts="tabletop, leg_fl, leg_fr, leg_bl, leg_br",
        envelope=_Envelope(
            size_x=(0.8, 1.8),
            size_y=(0.5, 1.1),
            size_z=(0.4, 1.0),
            bbox_min_y=(-0.05, 0.1),
            min_points=40,
        ),
    ),
    "shelf": _CaseSpec(
        label="three-tier shelf",
        target_name="eee_html_shelf",
        dimensions=(
            "three shelf boards 0.8m x 0.3m each; two side panels/uprights; "
            "total height 1.5m"
        ),
        parts="shelf_1, shelf_2, shelf_3, side_left, side_right",
        envelope=_Envelope(
            size_x=(0.5, 1.2),
            size_y=(1.0, 2.0),
            size_z=(0.2, 0.6),
            bbox_min_y=(-0.05, 0.1),
            min_points=40,
        ),
    ),
}


def _brief_a(case: str, spec: _CaseSpec) -> str:
    """Bounded, imperative design-round brief. Persisted in runtime events
    (normal behavior) but never copied into stdout/stderr or the evidence."""
    return (
        f"Design-review round for a {spec.label}. Do NOT touch Houdini in "
        "this run. Work efficiently: keep your reasoning SHORT and start "
        "producing the deliverable right away - a long internal monologue "
        "that never reaches the tool call is a failure. "
        "Step 1: reason briefly about the component list and the real-world "
        f"dimensions (meters): {spec.dimensions}. "
        "Step 2: author exactly one self-contained Three.js HTML document "
        f"that renders a static 3/4 view of the {spec.label}. Put ALL "
        "dimension constants in one const block at the top of the file, use "
        f"semantic part names ({spec.parts}), add a ground plane and a fixed "
        "camera that frames the whole asset, and use no animation loop and "
        "no external assets other than the Three.js CDN script. "
        f"Step 3: call the render_sketch tool once with sketch_name "
        f"'{case}_sketch' and that HTML document. "
        "Step 4: report the rendered PNG file path to the user and STOP - "
        "wait for the user's review. "
        "Hard constraint: in this run you must NOT call scratch_build, "
        "scratch_commit, cleanup_nodes, propose_modeling, or any other "
        "Houdini write tool. Rendering the sketch is the only side effect "
        "allowed in this round."
    )


def _brief_b(spec: _CaseSpec) -> str:
    """Bounded approval brief for the same-session build round."""
    return (
        "The sketch is approved - proceed with the Houdini build. Reuse the "
        "approved sketch and its dimension intent (same components, same "
        "real-world meter dimensions). "
        "Step 1: build the asset step by step with scratch_build in "
        "functional units - catalog node types only, literal parameter "
        "values, semantic part node names - iterating build -> observe -> "
        "adjust inside your run sandbox. Parameter names come from the "
        "catalog, never from memory: a box is sized with sizex/sizey/sizez "
        "(there is NO 'size' or 'radx' parm on a box), an xform translates "
        "with tx/ty/tz and rotates with rx/ry/rz, a tube rod is line+sweep, "
        "and parts are combined with merge and terminated in an output "
        "null. You may confirm other names with search_houdini_knowledge, "
        "but if the knowledge base has no answer, proceed with the "
        "parameter names listed in this message and validate through the "
        "geometry readback - never stall or stop because the knowledge "
        "base is unavailable. A wrong parameter name is a hard failure; "
        "refusing to build is also a failure. Every node in the "
        "sandbox is part "
        "of the committed asset: NEVER create test, probe, or throwaway "
        "nodes (a default 1x1x1 box at the origin is a failure), create "
        "only the asset's parts, and delete any node you no longer need "
        "before committing. The final scratch_build call must "
        "terminate the network in a single output null SOP with a semantic "
        "name so its cooked result is the complete assembled asset. "
        "Step 2: call verify_geometry on that output node. Only after it "
        "returns ok=true with an empty issues list, call scratch_commit "
        f"with target_parent_path=/obj, target_name={spec.target_name}, "
        "skip_structure_check=true (this is a multi-part asset) and no "
        "orientation checks. The assembled asset must rest on the ground: "
        "its minimum y coordinate must be 0 (within a centimeter), and its "
        "overall sizes must match the approved sketch dimensions. "
        "Step 3: finish by writing a short parameter breakdown record "
        "(part -> node type -> key dimensions in meters) and stop."
    )


def _status(message: str) -> None:
    """Emit one bounded status line (never provider content)."""
    print(f"html-journey: {message}", flush=True)


def _record_not_run(case: str | None, reason: str) -> int:
    """Bounded honest-not-run record (aligned with runtime_mvp_provider_e2e)."""
    hfs_raw = os.getenv("EEE_RUNTIME_MVP_HFS") or ""
    hfs_available = bool(hfs_raw.strip()) and Path(hfs_raw).is_file()
    credentials = any(
        bool((os.getenv(name) or "").strip()) for name in _pj._CREDENTIAL_VARS
    )
    print(
        json.dumps(
            {
                "acceptance": "html-session-journey",
                "status": "not_run",
                "reason": reason,
                "case": case,
                "hfs_available": hfs_available,
                "provider_credentials_available": credentials,
            },
            sort_keys=True,
        )
    )
    return 0


def _parse_args(argv: list[str] | None) -> tuple[str | None, bool]:
    parser = argparse.ArgumentParser(
        prog="html_session_journey",
        description="L4 two-round HTML-sketch agent session acceptance journey.",
    )
    parser.add_argument("--case", help="one of: chair, desk, shelf")
    parser.add_argument(
        "--sketch-only",
        action="store_true",
        help="B3 mode: run only the design round plus static HTML checks.",
    )
    args = parser.parse_args(argv)
    case = (args.case or "").strip().lower()
    return (case if case in _CASES else None), bool(args.sketch_only)


def _commit_preview_is_success(event: object, final_path: str) -> bool:
    """Case-parameterized twin of provider_journey._commit_preview_is_success
    (which hardcodes its own module-level final path)."""
    payload = getattr(event, "payload", None)
    if not isinstance(payload, Mapping):
        return False
    content = payload.get("content")
    if type(content) is not str:
        return False
    compact = "".join(content.split())
    expected = json.dumps(final_path)
    return (
        '"ok":true' in compact
        and '"committed":true' in compact
        and '"refused":false' in compact
        and f'"final_path":{expected}' in compact
        and '"receipt":{' in compact
    )


def _valid_render_result(result: Mapping[str, object]) -> bool:
    """Bounded validation of one parsed render_sketch success result."""
    return (
        result.get("ok") is True
        and type(result.get("image_path")) is str
        and type(result.get("image_bytes")) is int
        and result["image_bytes"] > 0
        and type(result.get("image_width")) is int
        and result["image_width"] >= 320
        and type(result.get("image_height")) is int
        and result["image_height"] >= 200
    )


def _png_on_disk(paths: object, image_path: str) -> bool:
    """Prove the reported PNG lives (non-empty) in the runtime sketches dir."""
    sketches_dir = (paths.artifacts_dir / "sketches").resolve()  # type: ignore[attr-defined]
    candidate = Path(image_path).resolve()
    try:
        candidate.relative_to(sketches_dir)
    except ValueError:
        return False
    try:
        return (
            candidate.suffix.lower() == ".png"
            and candidate.is_file()
            and candidate.stat().st_size > 0
        )
    except OSError:
        return False


def _geometry_within_envelope(stats: object, envelope: _Envelope) -> bool:
    """Loose bounding-box/point-count envelope check on bridge stats."""
    if not isinstance(stats, Mapping):
        return False
    points = stats.get("points")
    if type(points) is not int or points < envelope.min_points:
        return False
    bbox = stats.get("bbox")
    if not isinstance(bbox, Mapping):
        return False
    bbox_min = bbox.get("min")
    bbox_max = bbox.get("max")
    if (
        not isinstance(bbox_min, (list, tuple))
        or not isinstance(bbox_max, (list, tuple))
        or len(bbox_min) != 3
        or len(bbox_max) != 3
    ):
        return False
    values = [*bbox_min, *bbox_max]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        return False
    size = [bbox_max[i] - bbox_min[i] for i in range(3)]
    return (
        envelope.size_x[0] <= size[0] <= envelope.size_x[1]
        and envelope.size_y[0] <= size[1] <= envelope.size_y[1]
        and envelope.size_z[0] <= size[2] <= envelope.size_z[1]
        and envelope.bbox_min_y[0] <= bbox_min[1] <= envelope.bbox_min_y[1]
    )


def _bounded_bbox(stats: Mapping[str, object]) -> dict[str, list[float]]:
    bbox = stats["bbox"]
    return {
        "min": [round(float(v), 3) for v in bbox["min"]],  # type: ignore[index]
        "max": [round(float(v), 3) for v in bbox["max"]],  # type: ignore[index]
    }


async def _wait_completed(service: object, run: object, step: str) -> None:
    from eee_agent.runtime.models import RunStatus

    async with asyncio.timeout(_RUN_TIMEOUT_SECONDS):
        final = await service.wait_for_run(run.run_id)  # type: ignore[attr-defined]
    if final.status is not RunStatus.COMPLETED:
        raise _StepError(step, f"run finished as {final.status.value}")


def _check_run_a(paths: object, events: list[object], case: str) -> None:
    """Hard checks for the design round: sketch rendered, zero Houdini writes."""
    started, completed = _pj._tool_events(events, "render_sketch")
    if not started or not completed:
        raise _StepError("run_a", "render_sketch tool events are missing")
    render = next(
        (
            result
            for event in completed
            if (result := _pj._tool_result(event)) is not None
            and _valid_render_result(result)
        ),
        None,
    )
    if render is None:
        raise _StepError("run_a", "no valid render_sketch result")
    if not _png_on_disk(paths, render["image_path"]):  # type: ignore[arg-type]
        raise _StepError("run_a", "rendered PNG is missing from sketches dir")
    writes = sum(
        len(_pj._tool_events(events, name)[0])
        for name in _PRE_APPROVAL_FORBIDDEN_TOOLS
    )
    if writes != 0:
        raise _StepError("run_a", "pre-approval Houdini write tool was started")
    _status(f"run A ({case}) completed: sketch rendered, zero Houdini writes")


def _check_sketch_html(paths: object, case: str) -> None:
    """B3 static-quality checks on the persisted sketch HTML.

    Objective subset of the sketch checklist: self-contained Three.js,
    static (no animation/timer loop), dimension constants collected in a
    const block, bounded size.
    """
    html_path = (
        paths.artifacts_dir / "sketches" / f"{case}_sketch.html"  # type: ignore[attr-defined]
    )
    try:
        text = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise _StepError("sketch_html", "persisted sketch HTML is missing") from None
    if not 1024 <= len(text) <= 256 * 1024:
        raise _StepError("sketch_html", "sketch HTML size is out of bounds")
    lowered = text.lower()
    if "three" not in lowered:
        raise _StepError("sketch_html", "sketch HTML does not reference three.js")
    for token in ("requestanimationframe", "setinterval", "settimeout("):
        if token in lowered:
            raise _StepError("sketch_html", f"sketch HTML is not static ({token})")
    if not re.search(r"const\s+[A-Za-z_$][\w$]*\s*=", text):
        raise _StepError("sketch_html", "sketch HTML has no const dimension block")


async def _run_sketch_journey(
    paths: object, case: str, spec: _CaseSpec
) -> dict[str, object]:
    """B3 sketch-only round: run A checks plus the static HTML checklist."""
    from eee_agent.runtime.lock import RuntimeLock
    from eee_agent.runtime.service import RuntimeService

    with RuntimeLock(paths.lock_file):
        async with RuntimeService.open(paths, **_pj._service_wiring(paths)) as service:
            session = await service.create_session(f"HTML sketch stability ({case})")
            run_a = await service.start_run(session.session_id, _brief_a(case, spec))
            await _wait_completed(service, run_a, "run_a")
            events_a, _ = await _pj._replay_all(service, session.session_id)
            _check_run_a(paths, events_a, case)
            _check_sketch_html(paths, case)
    return {
        "case": case,
        "run_a_status": "completed",
        "render_sketch_ok": True,
        "png_ok": True,
        "pre_approval_houdini_writes": 0,
        "sketch_html_ok": True,
    }


def _check_run_b_build(events: list[object]) -> str:
    """Hard checks for build/verify/commit; returns the built output node name."""
    tool_events = {
        name: _pj._tool_events(events, name) for name in _RUN_B_REQUIRED_TOOLS
    }
    if any(
        not started or not completed for started, completed in tool_events.values()
    ):
        raise _StepError("run_b", "required build tool events are incomplete")

    build = next(
        (
            result
            for event in reversed(tool_events["scratch_build"][1])
            if (result := _pj._tool_result(event)) is not None
            and result.get("ok") is True
            and type(result.get("output_node")) is str
            and result["output_node"].rsplit("/", 1)[-1] != ""
            and result["output_node"] != result.get("sandbox_root")
            and result.get("geometry") is not None
        ),
        None,
    )
    if build is None:
        raise _StepError("run_b", "no successful scratch_build result")
    output_name = build["output_node"].rsplit("/", 1)[-1]  # type: ignore[union-attr]
    if not _NODE_NAME_RE.match(output_name):
        raise _StepError("run_b", "scratch_build output node name is invalid")

    verified = next(
        (
            result
            for event in tool_events["verify_geometry"][1]
            if (result := _pj._tool_result(event)) is not None
            and result.get("ok") is True
            and result.get("issues") == []
        ),
        None,
    )
    if verified is None:
        raise _StepError("run_b", "geometry verification did not pass")
    # B4: zero parameter-name errors tolerated in the translation round.
    for event in tool_events["scratch_build"][1]:
        result = _pj._tool_result(event)
        if result is not None and "parm not found" in str(result.get("message", "")):
            raise _StepError("run_b", "parm name error in scratch_build")
    return output_name


def _committed_child_paths(paths: object, run_id: str, final_path: str) -> list[str]:
    """Committed child paths of the final container from the task graph store.

    The build event's ``output_node`` is only the last-created node of one
    call (often a mid-chain part), so the whole-asset read-back node is
    derived from the recorded committed nodes instead. Returns [] when the
    store is unavailable or empty (caller falls back to the build result).
    """
    import sqlite3

    db = paths.state_dir / "app.sqlite"  # type: ignore[attr-defined]
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT committed_path FROM task_nodes "
                "WHERE run_id=? AND status='committed' AND committed_path IS NOT NULL",
                (run_id,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    prefix = final_path + "/"
    return [row[0] for row in rows if type(row[0]) is str and row[0].startswith(prefix)]


async def _check_run_b_scene(
    paths: object, run_b: object, spec: _CaseSpec, output_name: str
) -> dict[str, list[float]]:
    """Bridge read-back: committed path, geometry envelope, sandbox absence."""
    from eee_agent.houdini_bridge.read_only_provider import BridgeReadOnlyProvider
    from eee_agent.runtime.service import _sandbox_id_from_run

    final_path = f"/obj/{spec.target_name}"
    read_only = BridgeReadOnlyProvider(paths.state_dir)  # type: ignore[attr-defined]

    final_query = await read_only.query_scene([final_path])
    if final_query.get("ok") is not True or final_query.get("node_count") != 1:
        raise _StepError("final_query", "committed path is unavailable")

    # Whole-asset read-back. The asset is the geometry of the committed
    # output sink (commit places the display flag on it); stray probe nodes
    # the model left in the container are NOT part of the asset. The sink is
    # resolved via scratch.topology over the committed children recorded in
    # the task graph store. Two fallbacks: the union bbox of all committed
    # parts, then the build-result output node.
    candidates = _committed_child_paths(paths, run_b.run_id, final_path)  # type: ignore[attr-defined]
    geometry: object | None = None
    read_path = f"{final_path}/{output_name}"
    if candidates:
        from eee_agent.houdini_bridge.changeset_provider import (
            BridgeChangeSetProvider,
        )

        sink_path: str | None = None
        try:
            changeset = BridgeChangeSetProvider(paths.state_dir)  # type: ignore[attr-defined]
            topo = await changeset.scene_topology(paths=tuple(candidates[:64]))
            live = [node for node in topo.nodes if node.get("exists")]
            flagged = [node["path"] for node in live if node.get("display_flag")]
            sinks = [node["path"] for node in live if not node.get("outputs")]
            resolved = (flagged or sinks or [None])[0]
            if type(resolved) is str:
                sink_path = resolved
        except Exception:  # noqa: BLE001 - topology read is best-effort here
            sink_path = None
        if sink_path is not None:
            sink_stats = await read_only.geometry_stats(sink_path)
            if sink_stats.get("ok") is True:
                geometry = sink_stats.get("geometry_stats")
                read_path = sink_path
    if geometry is None and candidates:
        merged_points = 0
        merged_prims = 0
        mins = [float("inf")] * 3
        maxs = [float("-inf")] * 3
        parts_read = 0
        for child_path in candidates[:32]:
            part_stats = await read_only.geometry_stats(child_path)
            part = part_stats.get("geometry_stats")
            if part_stats.get("ok") is not True or not isinstance(part, Mapping):
                continue
            bbox = part.get("bbox") if isinstance(part, Mapping) else None
            if not isinstance(bbox, Mapping):
                continue
            bmin, bmax = bbox.get("min"), bbox.get("max")
            if not isinstance(bmin, (list, tuple)) or not isinstance(bmax, (list, tuple)):
                continue
            merged_points += int(part.get("points", 0))
            merged_prims += int(part.get("primitives", 0))
            for axis in range(3):
                mins[axis] = min(mins[axis], float(bmin[axis]))
                maxs[axis] = max(maxs[axis], float(bmax[axis]))
            parts_read += 1
        if parts_read:
            geometry = {
                "points": merged_points,
                "primitives": merged_prims,
                "bbox": {"min": mins, "max": maxs},
            }
            read_path = f"{final_path}/* ({parts_read} committed parts)"
    if geometry is None:
        final_stats = await read_only.geometry_stats(read_path)
        if final_stats.get("ok") is True:
            geometry = final_stats.get("geometry_stats")
    if not _geometry_within_envelope(geometry, spec.envelope):
        # Bounded diagnostic: the actual read-back numbers, never provider text.
        raise _StepError(
            "final_query",
            f"committed geometry outside envelope: node={read_path} "
            f"stats={str(geometry)[:300]}",
        )

    sandbox_path = f"/obj/eee_scratch_{_sandbox_id_from_run(run_b.run_id)}"  # type: ignore[attr-defined]
    sandbox_query = await read_only.query_scene([sandbox_path])
    # Absence is proven two ways: an empty query result, or the bridge's
    # not-found read error for the removed container (identical semantics to
    # provider_journey).
    sandbox_absent = (
        sandbox_query.get("ok") is True and sandbox_query.get("node_count") == 0
    ) or (
        sandbox_query.get("ok") is False
        and sandbox_query.get("code") == "bridge.houdini_read_failed"
        and "not found" in str(sandbox_query.get("message", "")).lower()
    )
    if not sandbox_absent:
        raise _StepError("sandbox_cleanup", "run sandbox still exists")
    return _bounded_bbox(geometry)  # type: ignore[arg-type]


async def _run_journey(
    paths: object, case: str, spec: _CaseSpec
) -> tuple[dict[str, object], str, int]:
    """Run both rounds. Returns (partial evidence, session id, run-split seq
    count) where the split is the event count after run A, used to attribute
    replayed events to run A vs run B after the restart."""
    from eee_agent.runtime.lock import RuntimeLock
    from eee_agent.runtime.service import RuntimeService

    final_path = f"/obj/{spec.target_name}"
    with RuntimeLock(paths.lock_file):
        async with RuntimeService.open(paths, **_pj._service_wiring(paths)) as service:
            session = await service.create_session(f"HTML sketch journey ({case})")

            # Run A: user request -> sketch only, zero Houdini writes.
            run_a = await service.start_run(session.session_id, _brief_a(case, spec))
            await _wait_completed(service, run_a, "run_a")
            events_a, _ = await _pj._replay_all(service, session.session_id)
            _check_run_a(paths, events_a, case)

            # Run B: user approval -> build, verify, commit.
            run_b = await service.start_run(session.session_id, _brief_b(spec))
            await _wait_completed(service, run_b, "run_b")
            events_all, _ = await _pj._replay_all(service, session.session_id)
            if len(events_all) <= len(events_a):
                raise _StepError("run_b", "event log did not grow for run B")
            events_b = events_all[len(events_a):]
            output_name = _check_run_b_build(events_b)

            commit_event = next(
                (
                    event
                    for event in _pj._tool_events(events_b, "scratch_commit")[1]
                    if _commit_preview_is_success(event, final_path)
                ),
                None,
            )
            if commit_event is None:
                raise _StepError("run_b", "no committed scratch result")

            final_bbox = await _check_run_b_scene(paths, run_b, spec, output_name)
            _status("run B completed: build verified, committed, scene confirmed")

    evidence: dict[str, object] = {
        "case": case,
        "run_a_status": "completed",
        "render_sketch_ok": True,
        "png_ok": True,
        "pre_approval_houdini_writes": 0,
        "run_b_status": "completed",
        "scratch_build_ok": True,
        "geometry_verified": True,
        "commit_committed": True,
        "final_path": final_path,
        "final_geometry_ok": True,
        "final_bbox": final_bbox,
        "sandbox_absent": True,
    }
    return evidence, session.session_id, len(events_a)


async def _restart_replay(paths: object, session_id: str, split: int) -> int:
    """Reopen the service and prove both rounds' tool events replay durably."""
    from eee_agent.runtime.lock import RuntimeLock
    from eee_agent.runtime.service import RuntimeService

    with RuntimeLock(paths.lock_file):
        async with RuntimeService.open(paths, **_pj._service_wiring(paths)) as service:
            events, last_seq = await _pj._replay_all(service, session_id)
            if type(last_seq) is not int or last_seq < 1:
                raise _StepError("replay", "replay last_seq is invalid")
            if len(events) <= split:
                raise _StepError("replay", "replayed log is shorter after restart")
            events_a, events_b = events[:split], events[split:]
            started, completed = _pj._tool_events(events_a, "render_sketch")
            if not started or not completed:
                raise _StepError("replay", "render_sketch events did not survive restart")
            for name in _RUN_B_REQUIRED_TOOLS:
                started, completed = _pj._tool_events(events_b, name)
                if not started or not completed:
                    raise _StepError(
                        "replay", "build tool events did not survive restart"
                    )
    _status("restart replay confirmed")
    return last_seq


def _write_evidence(path: Path, evidence: dict[str, object]) -> None:
    if frozenset(evidence) != _EVIDENCE_FIELDS:
        raise _StepError("evidence", "evidence fields are invalid")
    payload = json.dumps(evidence, sort_keys=True).encode("utf-8")
    if len(payload) > _MAX_EVIDENCE_BYTES:
        raise _StepError("evidence", "evidence record is too large")
    path.write_bytes(payload)


def _write_sketch_evidence(path: Path, evidence: dict[str, object]) -> None:
    if frozenset(evidence) != _SKETCH_EVIDENCE_FIELDS:
        raise _StepError("evidence", "sketch evidence fields are invalid")
    payload = json.dumps(evidence, sort_keys=True).encode("utf-8")
    if len(payload) > _MAX_EVIDENCE_BYTES:
        raise _StepError("evidence", "evidence record is too large")
    path.write_bytes(payload)


def main() -> int:
    case, sketch_only = _parse_args(None)
    if case is None:
        print("usage: html_session_journey.py --case {chair,desk,shelf} [--sketch-only]")
        return _record_not_run(None, "case_required")
    spec = _CASES[case]

    try:
        config = _pj._load_config()
    except _StepError as exc:
        # Environment gaps (missing runtime-home/HIP/evidence vars, missing
        # credentials, missing hython) are an honest not-run, not a failure.
        detail = exc.detail
        if "credentials" in detail:
            reason = "provider_credentials_unavailable"
        elif "hython" in detail:
            reason = "houdini_hfs_unavailable"
        else:
            reason = "environment_incomplete"
        return _record_not_run(case, reason)

    worker = None
    try:
        worker = _pj._start_worker(config)
        _pj._wait_for_bridge(config, worker)
        _status("secure bridge ready")
        if sketch_only:
            evidence = asyncio.run(_run_sketch_journey(config.paths, case, spec))
            evidence["scene_cleanup"] = _pj._finish_scene_cleanup(config, worker)
            _pj._close_worker_log(worker)
            worker = None
            _write_sketch_evidence(config.evidence_path, evidence)
        else:
            evidence, session_id, split = asyncio.run(
                _run_journey(config.paths, case, spec)
            )
            evidence["restart_replay_last_seq"] = asyncio.run(
                _restart_replay(config.paths, session_id, split)
            )
            evidence["scene_cleanup"] = _pj._finish_scene_cleanup(config, worker)
            _pj._close_worker_log(worker)
            worker = None
            _write_evidence(config.evidence_path, evidence)
    except _StepError as exc:
        print(
            f"html-journey: failed step={exc.step} detail={exc.detail}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - bounded, leak-free failure line
        from eee_agent.core import AgentException

        code = exc.error.code if isinstance(exc, AgentException) else None
        detail = code if code else f"unexpected {type(exc).__name__}"
        print(f"html-journey: failed step=journey detail={detail}", file=sys.stderr)
        return 1
    finally:
        if worker is not None:
            _pj._kill_worker(worker)
    _status("evidence written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
