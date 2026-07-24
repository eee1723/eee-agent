from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias



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
class UsageUpdated:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    # Prompt-cache metrics from the provider (0 when the provider doesn't
    # report them). cache_read = tokens served from the KV cache (free/cheap);
    # cache_creation = tokens written to the cache this turn. Tracking these
    # lets us observe whether the stable-prefix strategy is actually hitting
    # the provider's automatic cache (DeepSeek caches implicitly; Anthropic
    # needs explicit cache_control markers).
    cache_read: int = 0
    cache_creation: int = 0



ProviderEvent: TypeAlias = (
    ReasoningDelta
    | TextDelta
    | ToolCallStarted
    | ToolCallArgumentsDelta
    | UsageUpdated
)
