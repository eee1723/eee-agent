from __future__ import annotations

from typing import Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from eee_agent.providers.contracts import (
    ModelProfile,
    ProviderConnection,
    ProviderKind,
    ThinkingEffort,
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
        api_key = SecretStr(resolve_secret(connection.secret_ref))
        effort: Literal["high", "max"] | None = None
        if profile.effort is ThinkingEffort.HIGH:
            effort = "high"
        elif profile.effort is ThinkingEffort.MAX:
            effort = "max"
        if profile.thinking_enabled:
            if connection.base_url:
                return ChatAnthropic(
                    model_name=profile.model_name,
                    api_key=api_key,
                    base_url=connection.base_url,
                    max_tokens_to_sample=profile.max_output_tokens,
                    thinking={"type": "enabled"},
                    effort=effort,
                    output_version="v1",
                    timeout=connection.timeout_seconds,
                    max_retries=connection.max_retries,
                    stop=None,
                    stream_usage=True,
                )
            return ChatAnthropic(
                model_name=profile.model_name,
                api_key=api_key,
                max_tokens_to_sample=profile.max_output_tokens,
                thinking={"type": "enabled"},
                effort=effort,
                output_version="v1",
                timeout=connection.timeout_seconds,
                max_retries=connection.max_retries,
                stop=None,
                stream_usage=True,
            )
        if connection.base_url:
            return ChatAnthropic(
                model_name=profile.model_name,
                api_key=api_key,
                base_url=connection.base_url,
                max_tokens_to_sample=profile.max_output_tokens,
                output_version="v1",
                timeout=connection.timeout_seconds,
                max_retries=connection.max_retries,
                stop=None,
                stream_usage=True,
            )
        return ChatAnthropic(
            model_name=profile.model_name,
            api_key=api_key,
            max_tokens_to_sample=profile.max_output_tokens,
            output_version="v1",
            timeout=connection.timeout_seconds,
            max_retries=connection.max_retries,
            stop=None,
            stream_usage=True,
        )
