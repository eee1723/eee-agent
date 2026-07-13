from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from eee_agent.core.errors import AgentError


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    call_id: str
    name: str
    index: int


@dataclass(frozen=True, slots=True)
class ToolCallArgumentsDelta:
    call_id: str
    arguments_delta: str
    index: int


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    call_id: str
    name: str


@dataclass(frozen=True, slots=True)
class UsageUpdated:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class ModelCompleted:
    model_name: str | None


@dataclass(frozen=True, slots=True)
class ModelFailed:
    error: AgentError


ProviderEvent: TypeAlias = (
    ReasoningDelta
    | TextDelta
    | ToolCallStarted
    | ToolCallArgumentsDelta
    | ToolCallCompleted
    | UsageUpdated
    | ModelCompleted
    | ModelFailed
)
