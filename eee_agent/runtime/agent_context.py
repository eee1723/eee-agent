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


@dataclass(frozen=True, slots=True)
class RuntimeToolContext:
    """Per-run trusted context consumed by secure Runtime tools."""

    read_only: ReadOnlyProvider
    modeling: object | None = None

    def __post_init__(self) -> None:
        if self.read_only is None or not isinstance(self.read_only, ReadOnlyProvider):
            raise TypeError("RuntimeToolContext.read_only must implement ReadOnlyProvider")


__all__ = ["PlainData", "PlainValue", "ReadOnlyProvider", "RuntimeToolContext"]
