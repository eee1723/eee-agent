"""Central configuration, all from environment (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load .env from the project root (parent of the eee_agent package).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_REPO_ROOT, ".env"))


@dataclass(frozen=True)
class LlmConfig:
    provider: str
    model: str
    thinking_enabled: bool
    effort: str | None
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class VisionConfig:
    provider: str
    model: str
    max_image_bytes: int
    timeout_seconds: float


# Single authority for per-provider default models. ``llm_config`` accepts the
# full table; ``vision_config`` accepts only providers whose LangChain chat
# adapter is known to accept image input (DeepSeek is excluded).
_DEFAULT_MODELS = {
    "deepseek": "deepseek-v4-pro",
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4.1",
}
_VISION_PROVIDERS = frozenset({"anthropic", "openai"})

# Advisory-Vision request timeout bounds (seconds). Imported by the runtime
# wiring and the vision router so the default lives in exactly one place.
DEFAULT_VISION_TIMEOUT_SECONDS = 30.0
MIN_VISION_TIMEOUT_SECONDS = 0.1
MAX_VISION_TIMEOUT_SECONDS = 120.0


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
    if provider not in _DEFAULT_MODELS:
        raise ValueError(f"unknown EEE_LLM_PROVIDER: {provider!r}")
    model = os.getenv("EEE_LLM_MODEL") or _DEFAULT_MODELS[provider]
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


def vision_config() -> VisionConfig | None:
    """Return explicit advisory-Vision configuration, or keep it disabled.

    Vision never inherits ``EEE_LLM_PROVIDER``. Only providers already
    supported by the project registry and known to accept image input through
    their LangChain chat adapter are accepted here.
    """
    provider = (os.getenv("EEE_VISION_PROVIDER") or "").strip().lower()
    if not provider:
        return None
    if provider not in _VISION_PROVIDERS:
        raise ValueError(f"unknown EEE_VISION_PROVIDER: {provider!r}")
    model = (os.getenv("EEE_VISION_MODEL") or _DEFAULT_MODELS[provider]).strip()
    if not model:
        raise ValueError("EEE_VISION_MODEL must not be empty")
    try:
        max_image_bytes = int(
            os.getenv("EEE_VISION_MAX_IMAGE_BYTES", str(8 * 1024 * 1024))
        )
    except ValueError:
        raise ValueError("EEE_VISION_MAX_IMAGE_BYTES must be an integer") from None
    if not 1 <= max_image_bytes <= 16_777_216:
        raise ValueError("EEE_VISION_MAX_IMAGE_BYTES must be between 1 and 16777216")
    try:
        timeout_seconds = float(
            os.getenv(
                "EEE_VISION_TIMEOUT_SECONDS", str(DEFAULT_VISION_TIMEOUT_SECONDS)
            )
        )
    except ValueError:
        raise ValueError("EEE_VISION_TIMEOUT_SECONDS must be a number") from None
    if not MIN_VISION_TIMEOUT_SECONDS <= timeout_seconds <= MAX_VISION_TIMEOUT_SECONDS:
        raise ValueError("EEE_VISION_TIMEOUT_SECONDS must be between 0.1 and 120")
    return VisionConfig(
        provider=provider,
        model=model,
        max_image_bytes=max_image_bytes,
        timeout_seconds=timeout_seconds,
    )


def repo_root() -> str:
    return _REPO_ROOT


def recursion_limit() -> int:
    """langgraph step budget per run (env: EEE_RECURSION_LIMIT).

    Default 999 — generous headroom for long-horizon procedural modeling and for
    the recommended Claude swap (DeepSeek V4 Pro tends to over-iterate near the
    old 120 cap). Lower if the agent loops."""
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
