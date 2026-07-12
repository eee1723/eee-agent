"""Central configuration, all from environment (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load .env from the project root (parent of the eee_agent package).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_REPO_ROOT, ".env"))


@dataclass(frozen=True)
class RpcConfig:
    host: str
    port: int


@dataclass(frozen=True)
class LlmConfig:
    provider: str   # deepseek | anthropic | openai
    model: str


def rpc_config() -> RpcConfig:
    return RpcConfig(
        host=os.getenv("HOUDINI_RPC_HOST", "127.0.0.1"),
        port=int(os.getenv("HOUDINI_RPC_PORT", "18811")),
    )


def llm_config() -> LlmConfig:
    provider = os.getenv("EEE_LLM_PROVIDER", "deepseek").lower()
    defaults = {
        "deepseek": "deepseek-v4-pro",
        "anthropic": "claude-sonnet-5",
        "openai": "gpt-4.1",
    }
    model = os.getenv("EEE_LLM_MODEL") or defaults.get(provider, "deepseek-v4-pro")
    return LlmConfig(provider=provider, model=model)


def repo_root() -> str:
    return _REPO_ROOT


def recursion_limit() -> int:
    """langgraph step budget per run (env: EEE_RECURSION_LIMIT).

    Default 999 — generous headroom for long-horizon procedural modeling and for
    the recommended Claude swap (DeepSeek V4 Pro tends to over-iterate near the
    old 120 cap; see CLAUDE.md "Known limitation"). Lower if the agent loops."""
    return int(os.getenv("EEE_RECURSION_LIMIT", "999"))


def resolve_path(path: str) -> str:
    """Absolutize a relative path against the repo root.

    Without this, a path like "output/house.obj" is resolved by Houdini against
    *its own* cwd (e.g. C:\\Users\\tth17), not the project. Exports and .hip
    saves should land in the project.
    """
    if os.path.isabs(path):
        return path
    return os.path.join(_REPO_ROOT, path)
