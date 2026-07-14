from __future__ import annotations

import platform
import sys
from importlib import metadata

from eee_agent import __version__


TRACKED_DISTRIBUTIONS = (
    "deepagents",
    "langchain",
    "langchain-core",
    "langchain-openai",
    "langchain-anthropic",
    "langgraph",
    "langsmith",
    "openai",
    "anthropic",
    "rpyc",
    "aiosqlite",
    "langgraph-checkpoint-sqlite",
    "websockets",
)


def runtime_version_report() -> dict[str, object]:
    dependencies: dict[str, str | None] = {}
    for name in TRACKED_DISTRIBUTIONS:
        try:
            dependencies[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            dependencies[name] = None
    return {
        "eee_agent": __version__,
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "dependencies": dependencies,
    }
