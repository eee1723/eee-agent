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
    report: dict[str, object] = {
        "eee_agent": __version__,
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "dependencies": dependencies,
    }
    # Surface the active LLM provider/model and (if configured) the vision
    # provider/model so the panel can show which model produced a run. Frozen
    # into each run's model_snapshot_json (one call per start_run), so the
    # info survives reconnect/history replay. Defensive: a misconfigured env
    # must not crash the version report — fall back to "-" placeholders.
    try:
        from eee_agent.config import llm_config, vision_config
        llm = llm_config()
        report["llm_provider"] = llm.provider
        report["llm_model"] = llm.model
        vision = vision_config()
        if vision is not None:
            report["vision_provider"] = vision.provider
            report["vision_model"] = vision.model
        else:
            report["vision_provider"] = ""
            report["vision_model"] = ""
    except Exception:  # noqa: BLE001 — version report must never crash startup
        report.setdefault("llm_provider", "-")
        report.setdefault("llm_model", "-")
        report.setdefault("vision_provider", "")
        report.setdefault("vision_model", "")
    return report
