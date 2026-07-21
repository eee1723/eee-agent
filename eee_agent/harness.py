"""Explicit Deep Agents harness configuration.

Foundation keeps the existing tool surface for compatibility but explicitly
disables Deep Agents' auto-added general-purpose subagent. Capability-specific
subagents will be registered later with bounded tools and structured outputs.
"""
from __future__ import annotations

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)


_configured = False


def configure_deepagents_harness() -> None:
    """Register a no-general-purpose harness profile for known provider keys.

    Idempotent: the module-level ``_configured`` guard ensures repeated calls
    (e.g. across rebuilds in a single process) do not double-register.
    """
    global _configured
    if _configured:
        return
    profile = HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)
    )
    # DeepSeekV4ProviderAdapter is implemented with ChatAnthropic, so Deep
    # Agents resolves its harness provider as "anthropic". Standard OpenAI is
    # registered separately. Both are process-local EEE Agent policies.
    register_harness_profile("anthropic", profile)
    register_harness_profile("openai", profile)
    _configured = True
