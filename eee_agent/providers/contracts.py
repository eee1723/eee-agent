from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from langchain_core.language_models import BaseChatModel


class ProviderKind(StrEnum):
    DEEPSEEK = "deepseek"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class Transport(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class ThinkingEffort(StrEnum):
    HIGH = "high"
    MAX = "max"


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    streaming: bool
    thinking: bool
    tool_calling: bool
    structured_output: bool
    image_input: bool


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    connection_id: str
    provider: ProviderKind
    transport: Transport
    base_url: str | None
    secret_ref: str
    timeout_seconds: float = 120.0
    max_retries: int = 2

    def __post_init__(self) -> None:
        if not self.connection_id.strip():
            raise ValueError("connection_id must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")


@dataclass(frozen=True, slots=True)
class ModelProfile:
    profile_id: str
    connection_id: str
    model_name: str
    capabilities: ModelCapabilities
    thinking_enabled: bool = False
    effort: ThinkingEffort | None = None
    max_output_tokens: int = 8192

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.model_name.strip():
            raise ValueError("profile_id and model_name must not be empty")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.thinking_enabled and not self.capabilities.thinking:
            raise ValueError("thinking cannot be enabled for a non-thinking profile")


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    connection: ProviderConnection
    profile: ModelProfile
    model: BaseChatModel


class ProviderAdapter(Protocol):
    kind: ProviderKind

    def build(
        self, connection: ProviderConnection, profile: ModelProfile
    ) -> BaseChatModel: ...
