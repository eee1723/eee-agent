from langchain_core.messages import AIMessageChunk

from eee_agent.providers.events import (
    ReasoningDelta,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallStarted,
    UsageUpdated,
)
from eee_agent.providers.normalize import normalize_message_chunk


def test_normalize_standard_reasoning_text_tool_and_usage() -> None:
    chunk = AIMessageChunk(
        content=[
            {"type": "reasoning", "reasoning": "Inspect the component graph."},
            {"type": "text", "text": "I will inspect it."},
        ],
        tool_call_chunks=[
            {
                "name": "inspect_graph",
                "args": '{"path":',
                "id": "call_1",
                "index": 0,
                "type": "tool_call_chunk",
            }
        ],
        usage_metadata={"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
    )
    events = normalize_message_chunk(chunk)
    assert ReasoningDelta("Inspect the component graph.") in events
    assert TextDelta("I will inspect it.") in events
    assert ToolCallStarted("call_1", "inspect_graph", 0) in events
    assert ToolCallArgumentsDelta("call_1", '{"path":', 0) in events
    assert UsageUpdated(3, 5, 8) in events


def test_normalize_legacy_reasoning_content_during_migration() -> None:
    chunk = AIMessageChunk(
        content="answer",
        additional_kwargs={"reasoning_content": "legacy thought"},
    )
    assert normalize_message_chunk(chunk) == (
        ReasoningDelta("legacy thought"),
        TextDelta("answer"),
    )


def test_normalize_tool_chunk_without_name_emits_only_args_delta() -> None:
    # Streaming continuation chunk: carries args but no name/id, so it must not
    # synthesize a ToolCallStarted — only a ToolCallArgumentsDelta.
    chunk = AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"name": None, "args": '"path":', "id": None, "index": 1, "type": "tool_call_chunk"}
        ],
    )
    assert normalize_message_chunk(chunk) == (
        ToolCallArgumentsDelta("", '"path":', 1),
    )


def test_normalize_extracts_cache_metrics_when_reported() -> None:
    # When the provider reports cache metrics (Anthropic with cache_control, or
    # any provider that populates input_token_details), normalize must surface
    # them so we can observe KV-cache hit rate.
    chunk = AIMessageChunk(
        content="ok",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 50,
            "total_tokens": 1050,
            "input_token_details": {"cache_read": 800, "cache_creation": 200},
        },
    )
    events = normalize_message_chunk(chunk)
    assert UsageUpdated(1000, 50, 1050, cache_read=800, cache_creation=200) in events


def test_normalize_defaults_cache_to_zero_when_absent() -> None:
    # Most providers (incl. DeepSeek's auto-cache, which doesn't surface the
    # metric) omit input_token_details — cache fields default to 0.
    chunk = AIMessageChunk(
        content="ok",
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    events = normalize_message_chunk(chunk)
    assert UsageUpdated(10, 5, 15, cache_read=0, cache_creation=0) in events


def test_usage_updated_cache_defaults_to_zero() -> None:
    # The dataclass must keep backward compat: cache fields default to 0.
    u = UsageUpdated(3, 5, 8)
    assert u.cache_read == 0
    assert u.cache_creation == 0
