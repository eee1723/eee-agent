"""Tests for the pipeline tools: render_sketch + verify_geometry."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from eee_agent.runtime.agent_context import RuntimeToolContext
from eee_agent.runtime.sketch_tools import render_sketch, verify_geometry
from eee_agent.sketch import chrome as chrome_module
from eee_agent.sketch.chrome import ChromeSketchRenderer


def _run(coro):
    return asyncio.run(coro)


class _ReadOnly:
    def __init__(self, stats: dict | None = None) -> None:
        self._stats = stats or {
            "ok": True,
            "points": 8193,
            "prims": 7346,
            "bbox": {"min": [-8.0, 0.0, -8.0], "max": [8.0, 1.073, 8.0]},
        }

    async def scene_status(self):
        return {"ok": True}

    async def query_scene(self, node_paths):
        return {"ok": True, "nodes": []}

    async def inspect_workspace(self, workspace_id):
        return {"ok": True, "workspace": None}

    async def geometry_stats(self, node_path: str):
        return self._stats

    async def work_status(self, workspace_id):
        return {"ok": True, "components": []}


class _Knowledge:
    def search(self, query: str, *, limit: int = 5):
        return {"ok": True, "results": []}

    def get(self, entity_id: str, *, max_body_bytes: int = 8_000):
        return {"ok": True, "entity_id": entity_id, "body": ""}


class _Sketch:
    def __init__(self, result: dict | None = None, exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._result = result
        self._exc = exc

    async def render_sketch(self, *, html_content: str, sketch_name: str):
        self.calls.append({"html_content": html_content, "sketch_name": sketch_name})
        if self._exc is not None:
            raise self._exc
        return self._result


def _runtime(read_only=None, sketch="default"):
    if sketch == "default":
        sketch = _Sketch({"ok": True, "image_path": "/tmp/x.png"})
    return SimpleNamespace(
        context=RuntimeToolContext(
            read_only=read_only or _ReadOnly(),
            knowledge=_Knowledge(),
            sketch=sketch,
        )
    )


# ---------------------------------------------------------------- render_sketch

def test_render_sketch_rejects_invalid_input() -> None:
    runtime = _runtime()
    assert _run(render_sketch.coroutine("", "ok-name", runtime))["code"] == (
        "runtime.tool_input_invalid"
    )
    assert _run(render_sketch.coroutine("x" * (128 * 1024 + 1), "ok-name", runtime))[
        "code"
    ] == "runtime.tool_input_invalid"
    assert _run(render_sketch.coroutine("<html/>", "", runtime))["code"] == (
        "runtime.tool_input_invalid"
    )
    assert _run(render_sketch.coroutine("<html/>", "n" * 65, runtime))["code"] == (
        "runtime.tool_input_invalid"
    )


def test_render_sketch_fails_closed_without_provider() -> None:
    result = _run(render_sketch.coroutine("<html/>", "ok-name", _runtime(sketch=None)))
    assert result["ok"] is False
    assert result["code"] == "bridge.unavailable"


def test_render_sketch_passes_through_provider_result() -> None:
    sketch = _Sketch({"ok": True, "image_path": "/tmp/bike.png", "image_bytes": 1234})
    result = _run(render_sketch.coroutine("<html/>", "bike", _runtime(sketch=sketch)))
    assert result == {"ok": True, "image_path": "/tmp/bike.png", "image_bytes": 1234}
    assert sketch.calls == [{"html_content": "<html/>", "sketch_name": "bike"}]


def test_render_sketch_maps_provider_failure_and_malformed() -> None:
    raising = _run(
        render_sketch.coroutine("<html/>", "bike", _runtime(sketch=_Sketch(exc=RuntimeError())))
    )
    assert raising["code"] == "bridge.unavailable"
    malformed = _run(
        render_sketch.coroutine("<html/>", "bike", _runtime(sketch=_Sketch({"ok": "yes"})))
    )
    assert malformed["code"] == "bridge.unavailable"


def test_runtime_context_rejects_non_provider_sketch() -> None:
    with pytest.raises(TypeError, match="sketch"):
        RuntimeToolContext(
            read_only=_ReadOnly(), knowledge=_Knowledge(), sketch=object()
        )


# --------------------------------------------------------------- verify_geometry

def test_verify_geometry_rejects_bad_node_path() -> None:
    result = _run(verify_geometry.coroutine("obj/geo1", {"min_verts": 1}, _runtime()))
    assert result["code"] == "runtime.tool_input_invalid"


def test_verify_geometry_rejects_unknown_expect_keys() -> None:
    result = _run(
        verify_geometry.coroutine("/obj/geo1", {"min_triangles": 1}, _runtime())
    )
    assert result["code"] == "runtime.tool_input_invalid"


def test_verify_geometry_rejects_bad_bbox() -> None:
    bad_len = _run(
        verify_geometry.coroutine("/obj/geo1", {"bbox_min": [0, 0]}, _runtime())
    )
    assert bad_len["code"] == "runtime.tool_input_invalid"
    non_finite = _run(
        verify_geometry.coroutine(
            "/obj/geo1", {"bbox_max": [0, 0, float("inf")]}, _runtime()
        )
    )
    assert non_finite["code"] == "runtime.tool_input_invalid"


def test_verify_geometry_ok_path() -> None:
    result = _run(
        verify_geometry.coroutine(
            "/obj/eee_scratch_1/OUT",
            {
                "min_verts": 1000,
                "max_verts": 200000,
                "min_faces": 800,
                "bbox_min": [-9.0, -0.01, -9.0],
                "bbox_max": [9.0, 1.2, 9.0],
            },
            _runtime(),
        )
    )
    assert result["ok"] is True
    assert result["issues"] == []
    assert result["stats"]["verts"] == 8193
    assert result["stats"]["faces"] == 7346


def test_verify_geometry_reports_issues() -> None:
    result = _run(
        verify_geometry.coroutine(
            "/obj/eee_scratch_1/OUT",
            {"max_verts": 1000, "bbox_max": [1.0, 1.0, 1.0]},
            _runtime(),
        )
    )
    assert result["ok"] is False
    assert any("verts" in issue for issue in result["issues"])
    assert any("max" in issue for issue in result["issues"])


def test_verify_geometry_maps_provider_error() -> None:
    class _Boom(_ReadOnly):
        async def geometry_stats(self, node_path: str):
            raise ConnectionError("refused")

    result = _run(
        verify_geometry.coroutine(
            "/obj/geo1", {"min_verts": 1}, _runtime(read_only=_Boom())
        )
    )
    assert result["code"] == "bridge.unavailable"


# ----------------------------------------------------------- ChromeSketchRenderer

def test_renderer_rejects_bad_inputs(tmp_path) -> None:
    renderer = ChromeSketchRenderer(tmp_path)
    bad_name = _run(renderer.render_sketch(html_content="<html/>", sketch_name="bad name!"))
    assert bad_name["code"] == "sketch.input_invalid"
    oversized = _run(
        renderer.render_sketch(
            html_content="x" * (128 * 1024 + 1), sketch_name="ok-name"
        )
    )
    assert oversized["code"] == "sketch.input_invalid"


def test_renderer_reports_missing_browser(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chrome_module, "resolve_browser", lambda: None)
    renderer = ChromeSketchRenderer(tmp_path)
    result = _run(renderer.render_sketch(html_content="<html/>", sketch_name="bike"))
    assert result["ok"] is False
    assert result["code"] == "sketch.browser_missing"


def test_renderer_success_writes_html_and_returns_png(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chrome_module, "resolve_browser", lambda: "/fake/chrome")

    class _Proc:
        def __init__(self, png_path) -> None:
            self._png_path = png_path

        async def communicate(self):
            self._png_path.write_bytes(b"\x89PNG fake")
            return (b"", b"")

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kwargs):
        png_arg = next(arg for arg in cmd if arg.startswith("--screenshot="))
        from pathlib import Path

        return _Proc(Path(png_arg.split("=", 1)[1]))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    renderer = ChromeSketchRenderer(tmp_path)
    result = _run(renderer.render_sketch(html_content="<html>hi</html>", sketch_name="bike"))
    assert result["ok"] is True
    assert result["image_bytes"] == len(b"\x89PNG fake")
    assert (tmp_path / "bike.html").read_text(encoding="utf-8") == "<html>hi</html>"


def test_renderer_maps_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(chrome_module, "resolve_browser", lambda: "/fake/chrome")

    class _SlowProc:
        async def communicate(self):
            await asyncio.sleep(60)

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kwargs):
        return _SlowProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(chrome_module, "_RENDER_TIMEOUT_SECONDS", 0.01)
    renderer = ChromeSketchRenderer(tmp_path)
    result = _run(renderer.render_sketch(html_content="<html/>", sketch_name="bike"))
    assert result["code"] == "sketch.render_timeout"
