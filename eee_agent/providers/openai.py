from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from eee_agent.providers.contracts import (
    ModelProfile,
    ProviderConnection,
    ProviderKind,
    Transport,
)
from eee_agent.providers.secrets import resolve_secret


class OpenAIProviderAdapter:
    kind = ProviderKind.OPENAI

    def build(
        self,
        connection: ProviderConnection,
        profile: ModelProfile,
    ) -> BaseChatModel:
        if connection.transport is not Transport.OPENAI:
            raise ValueError("OpenAIProviderAdapter requires OpenAI transport")
        kwargs: dict[str, object] = {
            "model": profile.model_name,
            "api_key": resolve_secret(connection.secret_ref),
            "max_tokens": profile.max_output_tokens,
            "timeout": connection.timeout_seconds,
            "max_retries": connection.max_retries,
            "stream_usage": True,
        }
        if connection.base_url:
            kwargs["base_url"] = connection.base_url
        return ChatOpenAI(**kwargs)
