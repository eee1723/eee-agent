from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel

from eee_agent.providers.contracts import (
    ModelProfile,
    ProviderConnection,
    ProviderKind,
    Transport,
)
from eee_agent.providers.secrets import resolve_secret


class AnthropicProviderAdapter:
    kind = ProviderKind.ANTHROPIC

    def build(
        self,
        connection: ProviderConnection,
        profile: ModelProfile,
    ) -> BaseChatModel:
        if connection.transport is not Transport.ANTHROPIC:
            raise ValueError("AnthropicProviderAdapter requires Anthropic transport")
        kwargs: dict[str, object] = {
            "model": profile.model_name,
            "api_key": resolve_secret(connection.secret_ref),
            "max_tokens": profile.max_output_tokens,
            "timeout": connection.timeout_seconds,
            "max_retries": connection.max_retries,
            "output_version": "v1",
            "stream_usage": True,
        }
        if connection.base_url:
            kwargs["base_url"] = connection.base_url
        if profile.thinking_enabled:
            kwargs["thinking"] = {"type": "enabled"}
            if profile.effort:
                kwargs["effort"] = profile.effort.value
        return ChatAnthropic(**kwargs)
