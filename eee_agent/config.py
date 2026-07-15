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
    provider: str
    model: str
    thinking_enabled: bool
    effort: str | None
    max_output_tokens: int


def rpc_config() -> RpcConfig:
    return RpcConfig(
        host=os.getenv("HOUDINI_RPC_HOST", "127.0.0.1"),
        port=int(os.getenv("HOUDINI_RPC_PORT", "18811")),
    )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    raise ValueError(f"{name} must be enabled or disabled")


def llm_config() -> LlmConfig:
    provider = os.getenv("EEE_LLM_PROVIDER", "deepseek").strip().lower()
    defaults = {
        "deepseek": "deepseek-v4-pro",
        "anthropic": "claude-sonnet-5",
        "openai": "gpt-4.1",
    }
    if provider not in defaults:
        raise ValueError(f"unknown EEE_LLM_PROVIDER: {provider!r}")
    model = os.getenv("EEE_LLM_MODEL") or defaults[provider]
    thinking_enabled = _env_bool("EEE_LLM_THINKING", provider == "deepseek")
    _effort_env = (os.getenv("EEE_LLM_EFFORT") or "").strip().lower()
    effort = _effort_env or ("max" if provider == "deepseek" else None)
    if effort not in {None, "high", "max"}:
        raise ValueError("EEE_LLM_EFFORT must be high or max")
    try:
        max_output_tokens = int(os.getenv("EEE_LLM_MAX_TOKENS", "8192"))
    except ValueError:
        raise ValueError("EEE_LLM_MAX_TOKENS must be an integer") from None
    if max_output_tokens <= 0:
        raise ValueError("EEE_LLM_MAX_TOKENS must be positive")
    return LlmConfig(
        provider=provider,
        model=model,
        thinking_enabled=thinking_enabled,
        effort=effort,
        max_output_tokens=max_output_tokens,
    )


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


# --- Knowledge graph config ------------------------------------------------

# Shared, rebuildable cache for the offline Houdini documentation knowledge
# graph (design §8.2). Lives under %LOCALAPPDATA%, never inside the package or
# the repo, and is never committed. The Houdini build is baked into the path so
# different installs on the same machine do not collide.
_KB_CACHE_SUBPATH = os.path.join(
    "EEEAgent", "cache", "knowledge", "houdini", "21.0.440", "knowledge.sqlite3"
)


@dataclass(frozen=True)
class KnowledgeConfig:
    enabled: bool
    path: str
    hfs: str | None


def _default_kb_cache_path() -> str:
    """The default shared cache path under %LOCALAPPDATA% (design §8.2).

    When ``LOCALAPPDATA`` is unavailable the user home is used so the path stays
    absolute and never silently resolves against Houdini's cwd.
    """
    base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, _KB_CACHE_SUBPATH)


def knowledge_config() -> KnowledgeConfig:
    """Build the knowledge-tool configuration from the environment.

    - ``EEE_KB_ENABLED`` (default ``true``) is parsed with the same strict
      :func:`_env_bool` semantics as the other toggles; an illegal value raises
      at this boundary instead of silently disabling the tools.
    - ``EEE_KB_PATH`` overrides the cache location. A relative path resolves
      against :func:`repo_root`, never Houdini's cwd. An absolute path is kept
      verbatim. Unset -> the shared %LOCALAPPDATA% default.
    - ``EEE_HFS`` overrides the build/query source HFS (``None`` when unset).

    No database is opened here and no cache is built; this only resolves paths
    and the enabled flag.
    """
    enabled = _env_bool("EEE_KB_ENABLED", True)
    raw_path = os.getenv("EEE_KB_PATH")
    if raw_path and raw_path.strip():
        path = raw_path.strip()
        if not os.path.isabs(path):
            path = os.path.join(_REPO_ROOT, path)
    else:
        path = _default_kb_cache_path()
    raw_hfs = os.getenv("EEE_HFS")
    hfs = raw_hfs.strip() if raw_hfs and raw_hfs.strip() else None
    return KnowledgeConfig(enabled=enabled, path=path, hfs=hfs)
