from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from eee_agent.core.errors import AgentError, AgentException, ErrorCategory
from eee_agent.providers.contracts import (
    ModelProfile,
    ProviderConnection,
    ProviderKind,
    Transport,
)
from eee_agent.providers.secrets import resolve_secret


def _configuration_error(code: str, message: str) -> AgentException:
    """Structured provider-contract failure (same surface as DeepSeek)."""
    return AgentException(
        AgentError(
            code=code,
            category=ErrorCategory.PROVIDER_CONTRACT,
            message_for_user=message,
            requires_user_action=True,
        )
    )


class OpenAIProviderAdapter:
    kind = ProviderKind.OPENAI

    def build(
        self,
        connection: ProviderConnection,
        profile: ModelProfile,
    ) -> BaseChatModel:
        if connection.transport is not Transport.OPENAI:
            raise _configuration_error(
                "provider.transport_mismatch",
                "OpenAIProviderAdapter requires OpenAI transport.",
            )
        api_key = SecretStr(resolve_secret(connection.secret_ref))
        if connection.base_url:
            return ChatOpenAI(
                model=profile.model_name,
                api_key=api_key,
                base_url=connection.base_url,
                max_completion_tokens=profile.max_output_tokens,
                timeout=connection.timeout_seconds,
                max_retries=connection.max_retries,
                stream_usage=True,
            )
        return ChatOpenAI(
            model=profile.model_name,
            api_key=api_key,
            max_completion_tokens=profile.max_output_tokens,
            timeout=connection.timeout_seconds,
            max_retries=connection.max_retries,
            stream_usage=True,
        )
