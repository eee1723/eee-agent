from langchain_core.messages import AIMessageChunk

from eee_agent.cli import _legacy_stream_events


def test_legacy_stdio_mapping_keeps_reasoning_separate_from_text() -> None:
    chunk = AIMessageChunk(
        content=[
            {"type": "reasoning", "reasoning": "Check the graph."},
            {"type": "text", "text": "The graph is valid."},
        ],
        tool_call_chunks=[
            {
                "name": "inspect_graph",
                "args": "{}",
                "id": "call_1",
                "index": 0,
                "type": "tool_call_chunk",
            }
        ],
        usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
    )
    events = _legacy_stream_events(chunk)
    assert [event["type"] for event in events] == [
        "thinking",
        "token",
        "tool_call",
        "tool_call_args",
        "usage",
    ]
    assert events[0]["text"] == "Check the graph."
    assert events[1]["text"] == "The graph is valid."
    # Lock the exact dict shapes the Panel consumes (not just the type sequence):
    # a renamed/dropped field here would otherwise pass the type-only checks above.
    assert events[2] == {
        "type": "tool_call",
        "id": "call_1",
        "name": "inspect_graph",
        "index": 0,
        "args": "",
    }
    assert events[3] == {
        "type": "tool_call_args",
        "id": "call_1",
        "index": 0,
        "args": "{}",
    }
    assert events[4] == {
        "type": "usage",
        "tokens_in": 2,
        "tokens_out": 3,
        "tokens_total": 5,
    }
