from __future__ import annotations

from eee_agent.config import LlmConfig
from eee_agent.providers.anthropic import AnthropicProviderAdapter
from eee_agent.providers.contracts import (
    ModelCapabilities,
    ModelProfile,
    ProviderConnection,
    ProviderKind,
    ResolvedModel,
    ThinkingEffort,
    Transport,
)
from eee_agent.providers.deepseek_v4 import (
    DEEPSEEK_ANTHROPIC_URL,
    DeepSeekV4ProviderAdapter,
)
from eee_agent.providers.openai import OpenAIProviderAdapter
from eee_agent.providers.registry import ProviderRegistry


def build_default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(DeepSeekV4ProviderAdapter())
    registry.register(AnthropicProviderAdapter())
    registry.register(OpenAIProviderAdapter())
    return registry


def resolve_model(config: LlmConfig) -> ResolvedModel:
    provider = ProviderKind(config.provider)
    connection_id = f"{provider.value}-environment"
    if provider is ProviderKind.DEEPSEEK:
        transport = Transport.ANTHROPIC
        base_url = DEEPSEEK_ANTHROPIC_URL
        secret_ref = "env:DEEPSEEK_API_KEY"
        capabilities = ModelCapabilities(True, True, True, True, False)
    elif provider is ProviderKind.ANTHROPIC:
        transport = Transport.ANTHROPIC
        base_url = None
        secret_ref = "env:ANTHROPIC_API_KEY"
        capabilities = ModelCapabilities(True, True, True, True, False)
    elif provider is ProviderKind.OPENAI:
        transport = Transport.OPENAI
        base_url = None
        secret_ref = "env:OPENAI_API_KEY"
        capabilities = ModelCapabilities(True, False, True, True, False)
    else:
        raise ValueError(f"unsupported provider: {provider!r}")

    effort = ThinkingEffort(config.effort) if config.effort else None
    connection = ProviderConnection(
        connection_id=connection_id,
        provider=provider,
        transport=transport,
        base_url=base_url,
        secret_ref=secret_ref,
    )
    profile = ModelProfile(
        profile_id="primary",
        connection_id=connection_id,
        model_name=config.model,
        capabilities=capabilities,
        thinking_enabled=config.thinking_enabled,
        effort=effort,
        max_output_tokens=config.max_output_tokens,
    )
    return build_default_registry().resolve(connection, profile)
