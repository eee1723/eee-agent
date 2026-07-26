"""Trusted, provider-neutral context for Runtime agent tools.

This module intentionally contains no Houdini, bridge, persistence, or
filesystem imports.  RuntimeService owns construction of the context and
injects a bounded read-only provider for each Run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


PlainValue = str | int | float | bool | None
PlainData = PlainValue | Mapping[str, "PlainData"] | Sequence["PlainData"]


@runtime_checkable
class ReadOnlyProvider(Protocol):
    """The only live-scene capability exposed to Runtime tools."""

    async def scene_status(self) -> Mapping[str, PlainData]: ...

    async def query_scene(
        self, node_paths: list[str]
    ) -> Mapping[str, PlainData]: ...

    async def inspect_workspace(
        self, workspace_id: str
    ) -> Mapping[str, PlainData]: ...

    async def geometry_stats(self, node_path: str) -> Mapping[str, PlainData]: ...

    async def work_status(self, workspace_id: str) -> Mapping[str, PlainData]: ...


@runtime_checkable
class KnowledgeProvider(Protocol):
    """The bounded, read-only Houdini Knowledge Graph cache.

    Results come from a trusted local build (not the live Houdini process),
    so unlike the live-scene ReadOnlyProvider they do not need the bridge
    plain-value/budget validator: KnowledgeRuntime.search/get already return
    bounded plain JSON-serializable data and their own ``kb_unavailable``
    status when the cache is missing.
    """

    def search(self, query: str, *, limit: int = 5) -> dict[str, object]: ...

    def get(self, entity_id: str, *, max_body_bytes: int = 8_000) -> dict[str, object]: ...


@runtime_checkable
class SketchRenderProvider(Protocol):
    """Render a bounded HTML sketch to a PNG via a local headless browser.

    Implementations are local (no Houdini, no bridge) and must return a
    bounded plain result: ``{"ok": True, "image_path": ..., ...}`` on success
    or ``{"ok": False, "code": ..., "message": ...}`` on failure
    (browser missing / timeout / render failure).
    """

    async def render_sketch(
        self, *, html_content: str, sketch_name: str
    ) -> Mapping[str, PlainData]: ...


@dataclass(frozen=True, slots=True)
class RuntimeToolContext:
    """Per-run trusted context consumed by secure Runtime tools."""

    read_only: ReadOnlyProvider
    knowledge: KnowledgeProvider
    modeling: object | None = None
    scratch: object | None = None
    sketch: object | None = None
    task_graph: object | None = None

    def __post_init__(self) -> None:
        if self.read_only is None or not isinstance(self.read_only, ReadOnlyProvider):
            raise TypeError("RuntimeToolContext.read_only must implement ReadOnlyProvider")
        if self.knowledge is None or not isinstance(self.knowledge, KnowledgeProvider):
            raise TypeError("RuntimeToolContext.knowledge must implement KnowledgeProvider")
        if self.sketch is not None and not isinstance(self.sketch, SketchRenderProvider):
            raise TypeError("RuntimeToolContext.sketch must implement SketchRenderProvider")


__all__ = [
    "PlainData",
    "PlainValue",
    "KnowledgeProvider",
    "ReadOnlyProvider",
    "RuntimeToolContext",
    "SketchRenderProvider",
]
