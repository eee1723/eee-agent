from __future__ import annotations

from typing import Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from eee_agent.core.errors import AgentError, AgentException, ErrorCategory
from eee_agent.providers.contracts import (
    ModelProfile,
    ProviderConnection,
    ProviderKind,
    ThinkingEffort,
    Transport,
)
from eee_agent.providers.secrets import resolve_secret


DEEPSEEK_ANTHROPIC_URL = "https://api.deepseek.com/anthropic"
SUPPORTED_DEEPSEEK_MODELS = frozenset({"deepseek-v4-pro", "deepseek-v4-flash"})


class DeepSeekV4ProviderAdapter:
    kind = ProviderKind.DEEPSEEK

    def build(
        self,
        connection: ProviderConnection,
        profile: ModelProfile,
    ) -> BaseChatModel:
        if connection.transport is not Transport.ANTHROPIC:
            raise self._configuration_error(
                "provider.invalid_transport",
                "DeepSeek V4 must use the Anthropic transport.",
            )
        if (connection.base_url or "").rstrip("/") != DEEPSEEK_ANTHROPIC_URL:
            raise self._configuration_error(
                "provider.invalid_endpoint",
                "DeepSeek V4 must use the official Anthropic endpoint.",
            )
        if profile.model_name not in SUPPORTED_DEEPSEEK_MODELS:
            raise self._configuration_error(
                "provider.invalid_model",
                "Use an exact DeepSeek V4 model name; aliases are not accepted.",
            )
        if profile.capabilities.image_input:
            raise self._configuration_error(
                "provider.invalid_capability",
                "DeepSeek V4 Anthropic transport does not support image input.",
            )
        api_key = SecretStr(resolve_secret(connection.secret_ref))
        thinking = {"type": "enabled" if profile.thinking_enabled else "disabled"}
        effort: Literal["high", "max"] | None = None
        if profile.thinking_enabled:
            if profile.effort is ThinkingEffort.HIGH:
                effort = "high"
            elif profile.effort is ThinkingEffort.MAX:
                effort = "max"
        return ChatAnthropic(
            model_name=profile.model_name,
            base_url=DEEPSEEK_ANTHROPIC_URL,
            api_key=api_key,
            max_tokens_to_sample=profile.max_output_tokens,
            thinking=thinking,
            effort=effort,
            output_version="v1",
            timeout=connection.timeout_seconds,
            max_retries=connection.max_retries,
            stop=None,
            stream_usage=True,
        )

    @staticmethod
    def _configuration_error(code: str, message: str) -> AgentException:
        return AgentException(
            AgentError(
                code=code,
                category=ErrorCategory.PROVIDER_CONTRACT,
                message_for_user=message,
                requires_user_action=True,
            )
        )
